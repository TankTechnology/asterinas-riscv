# GMAC 阶段 5 真机验证（2026-10-07）

## 已通过的层级

- **PHY-only**：`target/megrez-physical-step1/gmac-phy-cycle-20261007-r2/`
  使用硬件 watchdog，完成平台初始化和有界 PHY 诊断，结果为
  `phy_status=pass`，随后观察到新的 U-Boot 周期。
- **正常注册**：`target/megrez-physical-step1/gmac-register-cycle-20261007-r1/`
  使用相同候选内核和 watchdog，观察到：

  ```text
  ASTERINAS_GMAC_DMA_CONTRACT version=0x52
  ASTERINAS_GMAC_SELECTED ... alias=1 version=0x52
  ASTERINAS_GMAC_READY ... alias=1 version=0x52
  ASTERINAS_GMAC_DATAPATH ... tx_submitted=0 tx_errors=0 rx_errors=0
  ```

  该周期没有主动发送网络事务，随后恢复到 U-Boot。

- **QEMU 数据路径**：`target/eic7700-qemu/phase3c-20261007/result.json`
  的 15/15 qtest、PHY-only、DMA/IRQ 注册、单帧 TX/RX、受控 TCP/UDP 和软件
  恢复均通过；这些结果不冒充真机网络结果。

## 真机网络事务边界

本轮没有把长时间网络压力带到开发板。固定 fixture gate 的几次启动前尝试均
记录为入口失败，fixture 请求数为 0：

- 代理模式：宿主代理上游不可用；
- 直连模式：旧 runner 在静默 U-Boot 提示符上等待，未加载内核；
- 修正 runner 后仍被地址冲突预检拦截。

这些记录分别保存在 `target/megrez-physical-step1/gmac-network-cycle-20261007-*`，
没有产生真实网络包，也没有形成 GMAC 失败证据。`megrez_gmac_gate.py` 已增加
打开串口后的安全 Ctrl-C 同步，相关 33 项测试通过。

历史上已有一份旧候选的有界物理网络事务记录，但它不绑定本次
`d6554801...` 内核，因此不作为当前候选的网络通过结论。

## 当前结论

阶段 5 的 PHY、正常注册、DMA/IRQ 初始化和错误计数边界已经完成并恢复验证；
当前候选的物理单包/fixture 事务仍标记为 **未验证**，原因是 gate 的地址冲突
预检和入口同步条件，而不是 GMAC/PHY 超时。开发板已恢复 RockOS，最新恢复
boot-id 为 `3d2052de-43de-4312-ba72-5c86b801b441`，认证 UID 0 可用。

后续若要补齐最后的物理单包层级，应先单独修正 gate 的“板端离线后地址预检”
流程，再安排一次最小单包测试；在此之前不进行长时间网络压测、桌面网络或
Firefox 流量测试。
