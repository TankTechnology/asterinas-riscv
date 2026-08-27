// SPDX-License-Identifier: MPL-2.0

//! Acyclic ownership tracking for AF_UNIX descriptors in flight.
//!
//! This module models the concrete strong-reference paths between UNIX socket
//! objects and the stream/datagram storage that can queue `SCM_RIGHTS` files.
//! The graph stores only weak node registrations. Edge guards temporarily own
//! node identities, while the queued files remain owned by protocol queues.

#![cfg_attr(
    not(ktest),
    expect(
        dead_code,
        reason = "SCM graph integration lands in follow-up B1 slices"
    )
)]

use core::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

use spin::Once;

use crate::prelude::*;

static NEXT_NODE_ID: AtomicU64 = AtomicU64::new(1);
static SCM_GRAPH: Once<SpinLock<ScmGraph>> = Once::new();

/// A socket object that can be carried by `SCM_RIGHTS`.
#[derive(Clone)]
pub(super) struct SocketNode(NodeHandle);

/// The shared storage owned by both endpoints of a stream connection.
#[derive(Clone)]
pub(super) struct StreamStorageNode(NodeHandle);

/// A listening stream socket's pending-connection queue.
#[derive(Clone)]
pub(super) struct StreamBacklogNode(NodeHandle);

/// A datagram receive queue that can own in-flight descriptors.
#[derive(Clone)]
pub(super) struct DatagramQueueNode(NodeHandle);

/// A failure to reserve one atomic batch of in-flight ownership edges.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ReservationError {
    /// At least one edge would close an ownership cycle.
    Cycle,
    /// An edge multiplicity cannot be represented.
    MultiplicityOverflow,
}

/// An existing strong ownership edge that lasts with its guard.
pub(super) struct PermanentEdge {
    edge: Option<Edge>,
}

/// An atomic batch of in-flight edges that has not reached a queue yet.
pub(super) struct ReservedEdges {
    edges: Vec<Edge>,
    is_active: bool,
}

/// An atomic batch of ownership edges attached to a queued control message.
pub(super) struct CommittedEdges {
    edges: Vec<Edge>,
    is_active: bool,
}

/// A closed graph-node interface shared by socket and storage identities.
pub(super) trait ScmGraphNode {
    fn node_handle(&self) -> &NodeHandle;
}

impl SocketNode {
    /// Creates a stable identity for one AF_UNIX socket object.
    pub(super) fn new() -> Self {
        Self(NodeHandle::new(NodeKind::Socket))
    }

    #[cfg(ktest)]
    pub(super) fn downgrade(&self) -> WeakNode {
        WeakNode(Arc::downgrade(&self.0.0))
    }
}

impl StreamStorageNode {
    /// Creates a stable identity for one shared stream storage object.
    pub(super) fn new() -> Self {
        Self(NodeHandle::new(NodeKind::StreamStorage))
    }

    #[cfg(ktest)]
    pub(super) fn downgrade(&self) -> WeakNode {
        WeakNode(Arc::downgrade(&self.0.0))
    }
}

impl StreamBacklogNode {
    /// Creates a stable identity for one stream backlog.
    pub(super) fn new() -> Self {
        Self(NodeHandle::new(NodeKind::StreamBacklog))
    }
}

impl DatagramQueueNode {
    /// Creates a stable identity for one datagram receive queue.
    pub(super) fn new() -> Self {
        Self(NodeHandle::new(NodeKind::DatagramQueue))
    }

    #[cfg(ktest)]
    pub(super) fn downgrade(&self) -> WeakNode {
        WeakNode(Arc::downgrade(&self.0.0))
    }
}

impl ScmGraphNode for SocketNode {
    fn node_handle(&self) -> &NodeHandle {
        &self.0
    }
}

impl ScmGraphNode for StreamStorageNode {
    fn node_handle(&self) -> &NodeHandle {
        &self.0
    }
}

impl ScmGraphNode for StreamBacklogNode {
    fn node_handle(&self) -> &NodeHandle {
        &self.0
    }
}

impl ScmGraphNode for DatagramQueueNode {
    fn node_handle(&self) -> &NodeHandle {
        &self.0
    }
}

impl PermanentEdge {
    /// Adds one multiplicity-preserving edge until the returned guard drops.
    pub(super) fn new(
        from: &impl ScmGraphNode,
        to: &impl ScmGraphNode,
    ) -> Result<Self, ReservationError> {
        let edge = Edge::new(from.node_handle(), to.node_handle());
        scm_graph()
            .lock()
            .add_acyclic_edge(&edge, EdgeClass::Permanent)?;

        Ok(Self { edge: Some(edge) })
    }

    /// Removes this ownership edge before its enclosing object drops.
    pub(super) fn detach(mut self) {
        self.remove();
    }

    fn remove(&mut self) {
        let Some(edge) = self.edge.take() else {
            return;
        };
        scm_graph().lock().remove_edge(&edge, EdgeClass::Permanent);
    }
}

impl Drop for PermanentEdge {
    fn drop(&mut self) {
        self.remove();
    }
}

impl ReservedEdges {
    /// Reserves a packet's UNIX-socket edges as one all-or-nothing operation.
    ///
    /// Each proposed edge is `target_storage -> passed_socket`. Earlier
    /// reservations in the batch and all concurrent reservations participate
    /// in reachability. Duplicate descriptors retain their multiplicity.
    pub(super) fn try_new(
        target_storage: &impl ScmGraphNode,
        passed_sockets: &[SocketNode],
    ) -> Result<Self, ReservationError> {
        let target = target_storage.node_handle();
        let edges = passed_sockets
            .iter()
            .map(|socket| Edge::new(target, socket.node_handle()))
            .collect::<Vec<_>>();

        scm_graph().lock().reserve_edges(&edges)?;
        Ok(Self {
            edges,
            is_active: true,
        })
    }

    /// Converts every reservation to a committed queue edge atomically.
    pub(super) fn commit(mut self) -> CommittedEdges {
        scm_graph().lock().commit_edges(&self.edges);
        self.is_active = false;

        CommittedEdges {
            edges: core::mem::take(&mut self.edges),
            is_active: true,
        }
    }

    /// Rolls back every reservation in this batch.
    pub(super) fn rollback(mut self) {
        self.remove();
    }

    fn remove(&mut self) {
        if !self.is_active {
            return;
        }
        scm_graph()
            .lock()
            .remove_edges(&self.edges, EdgeClass::Reserved);
        self.is_active = false;
    }
}

impl Drop for ReservedEdges {
    fn drop(&mut self) {
        self.remove();
    }
}

impl CommittedEdges {
    /// Detaches every edge when a queued control message is consumed or drained.
    pub(super) fn detach(mut self) {
        self.remove();
    }

    fn remove(&mut self) {
        if !self.is_active {
            return;
        }
        scm_graph()
            .lock()
            .remove_edges(&self.edges, EdgeClass::Committed);
        self.is_active = false;
    }
}

impl Drop for CommittedEdges {
    fn drop(&mut self) {
        self.remove();
    }
}

#[derive(Clone)]
pub(super) struct NodeHandle(Arc<NodeIdentity>);

impl NodeHandle {
    fn new(kind: NodeKind) -> Self {
        let id = next_node_id();
        let identity = Arc::new(NodeIdentity { id });
        scm_graph()
            .lock()
            .register_node(id, kind, Arc::downgrade(&identity));
        Self(identity)
    }

    fn id(&self) -> NodeId {
        self.0.id
    }
}

struct NodeIdentity {
    id: NodeId,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct NodeId(u64);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NodeKind {
    Socket,
    StreamStorage,
    StreamBacklog,
    DatagramQueue,
}

#[derive(Clone)]
struct Edge {
    from: NodeHandle,
    to: NodeHandle,
}

impl Edge {
    fn new(from: &NodeHandle, to: &NodeHandle) -> Self {
        Self {
            from: from.clone(),
            to: to.clone(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EdgeClass {
    Permanent,
    Reserved,
    Committed,
}

struct RegisteredNode {
    kind: NodeKind,
    identity: Weak<NodeIdentity>,
}

#[derive(Default)]
struct EdgeCounts {
    permanent: usize,
    reserved: usize,
    committed: usize,
}

impl EdgeCounts {
    fn get(&self, class: EdgeClass) -> usize {
        match class {
            EdgeClass::Permanent => self.permanent,
            EdgeClass::Reserved => self.reserved,
            EdgeClass::Committed => self.committed,
        }
    }

    fn get_mut(&mut self, class: EdgeClass) -> &mut usize {
        match class {
            EdgeClass::Permanent => &mut self.permanent,
            EdgeClass::Reserved => &mut self.reserved,
            EdgeClass::Committed => &mut self.committed,
        }
    }

    fn total(&self) -> usize {
        self.permanent
            .saturating_add(self.reserved)
            .saturating_add(self.committed)
    }

    fn try_increment(&mut self, class: EdgeClass) -> Result<(), ReservationError> {
        self.total()
            .checked_add(1)
            .ok_or(ReservationError::MultiplicityOverflow)?;
        let count = self.get_mut(class);
        *count = count
            .checked_add(1)
            .ok_or(ReservationError::MultiplicityOverflow)?;
        Ok(())
    }

    fn decrement(&mut self, class: EdgeClass) {
        let count = self.get_mut(class);
        debug_assert!(*count > 0, "SCM graph edge guard removed twice");
        *count = count.saturating_sub(1);
    }
}

#[derive(Default)]
struct ScmGraph {
    nodes: BTreeMap<NodeId, RegisteredNode>,
    edges: BTreeMap<NodeId, BTreeMap<NodeId, EdgeCounts>>,
}

impl ScmGraph {
    fn register_node(&mut self, id: NodeId, kind: NodeKind, identity: Weak<NodeIdentity>) {
        let old = self.nodes.insert(id, RegisteredNode { kind, identity });
        debug_assert!(old.is_none(), "SCM graph node IDs must be unique");
        self.prune_dead_nodes();
    }

    fn add_edge(&mut self, edge: &Edge, class: EdgeClass) -> Result<(), ReservationError> {
        self.edges
            .entry(edge.from.id())
            .or_default()
            .entry(edge.to.id())
            .or_default()
            .try_increment(class)
    }

    fn add_acyclic_edge(&mut self, edge: &Edge, class: EdgeClass) -> Result<(), ReservationError> {
        if self.reaches(edge.to.id(), edge.from.id()) {
            return Err(ReservationError::Cycle);
        }
        self.add_edge(edge, class)
    }

    fn reserve_edges(&mut self, edges: &[Edge]) -> Result<(), ReservationError> {
        for (added_count, edge) in edges.iter().enumerate() {
            if let Err(error) = self.add_acyclic_edge(edge, EdgeClass::Reserved) {
                self.remove_edges(&edges[..added_count], EdgeClass::Reserved);
                return Err(error);
            }
        }
        Ok(())
    }

    fn commit_edges(&mut self, edges: &[Edge]) {
        for edge in edges {
            let counts = self
                .edges
                .get_mut(&edge.from.id())
                .and_then(|targets| targets.get_mut(&edge.to.id()))
                .expect("a committed SCM edge must have a reservation");
            counts.decrement(EdgeClass::Reserved);
            debug_assert!(counts.committed < usize::MAX);
            counts.committed = counts.committed.saturating_add(1);
        }
    }

    fn remove_edges(&mut self, edges: &[Edge], class: EdgeClass) {
        for edge in edges {
            self.remove_edge(edge, class);
        }
        self.prune_dead_nodes();
    }

    fn remove_edge(&mut self, edge: &Edge, class: EdgeClass) {
        let from = edge.from.id();
        let to = edge.to.id();
        let mut remove_source = false;

        if let Some(targets) = self.edges.get_mut(&from) {
            let mut remove_target = false;
            if let Some(counts) = targets.get_mut(&to) {
                counts.decrement(class);
                remove_target = counts.total() == 0;
            } else {
                debug_assert!(false, "SCM graph edge guard has no matching edge");
            }
            if remove_target {
                targets.remove(&to);
            }
            remove_source = targets.is_empty();
        } else {
            debug_assert!(false, "SCM graph edge guard has no source node");
        }

        if remove_source {
            self.edges.remove(&from);
        }
    }

    fn reaches(&self, start: NodeId, target: NodeId) -> bool {
        if start == target {
            return true;
        }

        let mut visited = BTreeSet::new();
        let mut pending = vec![start];
        while let Some(node) = pending.pop() {
            if !visited.insert(node) {
                continue;
            }
            let Some(next_nodes) = self.edges.get(&node) else {
                continue;
            };
            for (next, counts) in next_nodes {
                if counts.total() == 0 {
                    continue;
                }
                if *next == target {
                    return true;
                }
                if !visited.contains(next) {
                    pending.push(*next);
                }
            }
        }
        false
    }

    fn prune_dead_nodes(&mut self) {
        self.nodes
            .retain(|_, node| node.identity.strong_count() != 0);
    }
}

fn scm_graph() -> &'static SpinLock<ScmGraph> {
    SCM_GRAPH.call_once(|| SpinLock::new(ScmGraph::default()))
}

fn next_node_id() -> NodeId {
    let id = NEXT_NODE_ID
        .try_update(
            AtomicOrdering::Relaxed,
            AtomicOrdering::Relaxed,
            |current| current.checked_add(1),
        )
        .expect("SCM graph exhausted its node ID space");
    NodeId(id)
}

#[cfg(ktest)]
pub(super) struct WeakNode(Weak<NodeIdentity>);

#[cfg(ktest)]
impl WeakNode {
    pub(super) fn is_alive(&self) -> bool {
        self.0.strong_count() != 0
    }
}

#[cfg(ktest)]
pub(super) fn permanent_edge_count(from: &impl ScmGraphNode, to: &impl ScmGraphNode) -> usize {
    scm_graph()
        .lock()
        .edges
        .get(&from.node_handle().id())
        .and_then(|targets| targets.get(&to.node_handle().id()))
        .map(|counts| counts.get(EdgeClass::Permanent))
        .unwrap_or(0)
}

#[cfg(ktest)]
mod test;
