// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use ostd::prelude::ktest;

use super::*;
use crate::thread::{Thread, kernel_thread::ThreadOptions};

#[ktest]
fn accepts_unrelated_acyclic_edges() {
    let queue = DatagramQueueNode::new();
    let socket = SocketNode::new();

    let reservation = ReservedEdges::try_new(&queue, &[socket.clone()]).unwrap();
    assert_eq!(edge_count(&queue, &socket, EdgeClass::Reserved), 1);
    reservation.rollback();
    assert_eq!(edge_count(&queue, &socket, EdgeClass::Reserved), 0);

    let committed = ReservedEdges::try_new(&queue, &[socket.clone()])
        .unwrap()
        .commit();
    assert_eq!(edge_count(&queue, &socket, EdgeClass::Committed), 1);
    committed.detach();
}

#[ktest]
fn rejects_direct_and_long_mixed_cycles() {
    let direct_socket = SocketNode::new();
    let direct_storage = StreamStorageNode::new();
    let _direct_owner = PermanentEdge::new(&direct_socket, &direct_storage).unwrap();
    assert_eq!(
        ReservedEdges::try_new(&direct_storage, &[direct_socket])
            .err()
            .unwrap(),
        ReservationError::Cycle
    );

    let queued_socket = SocketNode::new();
    let queued_storage = StreamStorageNode::new();
    let queued = ReservedEdges::try_new(&queued_storage, &[queued_socket.clone()])
        .unwrap()
        .commit();
    assert_eq!(
        PermanentEdge::new(&queued_socket, &queued_storage)
            .err()
            .unwrap(),
        ReservationError::Cycle
    );
    queued.detach();

    let first_socket = SocketNode::new();
    let stream_storage = StreamStorageNode::new();
    let second_socket = SocketNode::new();
    let datagram_queue = DatagramQueueNode::new();
    let _first_owner = PermanentEdge::new(&first_socket, &stream_storage).unwrap();
    let queued = ReservedEdges::try_new(&stream_storage, &[second_socket.clone()])
        .unwrap()
        .commit();
    let _second_owner = PermanentEdge::new(&second_socket, &datagram_queue).unwrap();

    assert_eq!(
        ReservedEdges::try_new(&datagram_queue, &[first_socket])
            .err()
            .unwrap(),
        ReservationError::Cycle
    );
    queued.detach();
}

#[ktest]
fn preserves_duplicate_edge_multiplicity() {
    let socket = SocketNode::new();
    let storage = StreamStorageNode::new();
    let first = PermanentEdge::new(&socket, &storage).unwrap();
    let second = PermanentEdge::new(&socket, &storage).unwrap();

    assert_eq!(edge_count(&socket, &storage, EdgeClass::Permanent), 2);
    first.detach();
    assert_eq!(edge_count(&socket, &storage, EdgeClass::Permanent), 1);
    second.detach();
    assert_eq!(edge_count(&socket, &storage, EdgeClass::Permanent), 0);
}

#[ktest]
fn graph_lock_is_atomic_context_safe() {
    fn assert_spin_lock(_: &'static SpinLock<ScmGraph>) {}

    assert_spin_lock(scm_graph());
}

#[ktest]
fn rolls_back_the_whole_batch_on_cycle() {
    let storage = StreamStorageNode::new();
    let safe_socket = SocketNode::new();
    let cyclic_socket = SocketNode::new();
    let _owner = PermanentEdge::new(&cyclic_socket, &storage).unwrap();

    assert_eq!(
        ReservedEdges::try_new(&storage, &[safe_socket.clone(), cyclic_socket])
            .err()
            .unwrap(),
        ReservationError::Cycle
    );
    assert_eq!(edge_count(&storage, &safe_socket, EdgeClass::Reserved), 0);
}

#[ktest]
fn concurrent_opposite_reservations_cannot_both_succeed() {
    let first_storage = StreamStorageNode::new();
    let second_storage = StreamStorageNode::new();
    let first_socket = SocketNode::new();
    let second_socket = SocketNode::new();
    let _first_owner = PermanentEdge::new(&first_socket, &first_storage).unwrap();
    let _second_owner = PermanentEdge::new(&second_socket, &second_storage).unwrap();

    let is_starting = Arc::new(AtomicBool::new(false));
    let attempts = Arc::new(AtomicUsize::new(0));
    let successes = Arc::new(AtomicUsize::new(0));
    let first_thread = reserve_until_both_attempted(
        second_storage,
        first_socket,
        is_starting.clone(),
        attempts.clone(),
        successes.clone(),
    );
    let second_thread = reserve_until_both_attempted(
        first_storage,
        second_socket,
        is_starting.clone(),
        attempts,
        successes.clone(),
    );

    is_starting.store(true, Ordering::Release);
    first_thread.join();
    second_thread.join();
    assert_eq!(successes.load(Ordering::Acquire), 1);
}

#[ktest]
fn weak_registration_does_not_keep_a_node_alive() {
    let node = SocketNode::new();
    let id = node.node_handle().id();
    let weak = Arc::downgrade(&node.node_handle().0);
    assert!(is_registered(id, NodeKind::Socket));

    drop(node);
    assert!(weak.upgrade().is_none());
    scm_graph().lock().prune_dead_nodes();
    assert!(!is_registered(id, NodeKind::Socket));
}

fn reserve_until_both_attempted(
    storage: StreamStorageNode,
    socket: SocketNode,
    is_starting: Arc<AtomicBool>,
    attempts: Arc<AtomicUsize>,
    successes: Arc<AtomicUsize>,
) -> Arc<Thread> {
    ThreadOptions::new(move || {
        while !is_starting.load(Ordering::Acquire) {
            Thread::yield_now();
        }
        let reservation = ReservedEdges::try_new(&storage, &[socket]).ok();
        if reservation.is_some() {
            successes.fetch_add(1, Ordering::AcqRel);
        }
        attempts.fetch_add(1, Ordering::AcqRel);
        while attempts.load(Ordering::Acquire) != 2 {
            Thread::yield_now();
        }
        drop(reservation);
    })
    .spawn()
}

fn edge_count(from: &impl ScmGraphNode, to: &impl ScmGraphNode, class: EdgeClass) -> usize {
    scm_graph()
        .lock()
        .edges
        .get(&from.node_handle().id())
        .and_then(|targets| targets.get(&to.node_handle().id()))
        .map(|counts| counts.get(class))
        .unwrap_or(0)
}

fn is_registered(id: NodeId, kind: NodeKind) -> bool {
    scm_graph()
        .lock()
        .nodes
        .get(&id)
        .is_some_and(|node| node.kind == kind && node.identity.strong_count() != 0)
}
