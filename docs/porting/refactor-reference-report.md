# E1–E5 重构引用报告

扫描规则：代码、Makefile、overlay 和测试引用视为活动引用；文档引用只用于追溯。

| 路径 | tracked | 活动引用 | 文档引用 | 处理建议 |
| --- | ---: | ---: | ---: | --- |
| `tools/riscv/debian/rootfs/README.md` | yes | 29 | 90 | 保留 |
| ↳ 活动引用 |  | `Cargo.toml, book/book/kernel/index.html, book/book/print.html, book/book/to-contribute/coding-guidelines/for-documentation/general-style.html, book/book/to-contribute/coding-guidelines/for-documentation/index.html, book/book/to-contribute/coding-guidelines/index.html, osdk/Cargo.toml, osdk/deps/frame-allocator/Cargo.toml` |  |  |
| `tools/riscv/debian/rootfs/__init__.py` | yes | 113 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_m5_marionette_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m3_gate.py, tools/riscv/debian/rootfs/desktop_m4_gate.py, tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m6_browser_gate.py` |  |  |
| `tools/riscv/debian/rootfs/asterinas_clock_sync.conf` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_megrez_clock_sync.py` |  |  |
| `tools/riscv/debian/rootfs/browser_composite_capture.py` | yes | 7 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_composite_capture.py, tools/riscv/tests/test_browser_daily_use_gate.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/browser_daily_use_contract.py` | yes | 13 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_daily_use_upload.py, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/megrez_firefox_daily_use.py, tools/riscv/megrez_firefox_daily_use_report.py, tools/riscv/tests/test_browser_daily_use_contract.py` |  |  |
| `tools/riscv/debian/rootfs/browser_daily_use_gate.py` | yes | 6 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/debian/rootfs/browser_daily_use_upload.py` | yes | 7 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/megrez_firefox_daily_use.py, tools/riscv/tests/test_browser_daily_use_upload.py, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/debian/rootfs/browser_interaction_perf.py` | yes | 8 | 7 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/debian/rootfs/physical_graphics_gate.py, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_interaction_perf.py, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/browser_latency_contract.py` | yes | 3 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_perf_capture.py, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/browser_m5.py` | yes | 23 | 13 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_m5_marionette_gate.py, tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_perf_capture.py, tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/browser_web_marionette_gate.py, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh` |  |  |
| `tools/riscv/debian/rootfs/browser_m5_firefox.sh` | yes | 4 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_m5_runtime_gate.py, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_signed_sources.py` |  |  |
| `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py` | yes | 19 | 9 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_perf_capture.py, tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/browser_web_marionette_gate.py, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/debian/rootfs/firefox_drm_scene_gate.py, tools/riscv/debian/rootfs/firefox_marionette_probe_overlay.json` |  |  |
| `tools/riscv/debian/rootfs/browser_m5_network_observer.sh` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_m5_runtime_gate.py` |  |  |
| `tools/riscv/debian/rootfs/browser_m5_probe.html` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_m5.py` |  |  |
| `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_m5_window_observer.sh` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_m5_runtime_gate.py` |  |  |
| `tools/riscv/debian/rootfs/browser_perf_capture.py` | yes | 7 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_composite_capture.py, tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py, tools/riscv/tests/test_browser_perf_capture.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/browser_performance_provenance.py` | yes | 6 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_performance_provenance.py, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_startup_cache_check.py` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/browser_system_time.py` | yes | 8 | 8 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_composite_capture.py, tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_perf_capture.py, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py, tools/riscv/tests/test_browser_system_time.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web.service` | yes | 3 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_contract.py` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/firefox_fast_check.sh, tools/riscv/megrez_gmac_gate.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | yes | 0 | 4 | 候选删除，需人工确认动态引用 |
| `tools/riscv/debian/rootfs/browser_web_diagnostic.conf` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_evidence.service` | yes | 3 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_evidence.sh` | yes | 5 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/browser_web_firefox.sh, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_firefox.sh` | yes | 4 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_marionette_gate.py` | yes | 10 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_daily_use_gate.py, tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/megrez_firefox_browse.py, tools/riscv/tests/test_browser_daily_use_gate.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_online_rootfs_check.py` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_qemu_gate.py` | yes | 3 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_startup_profile.py, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_timeline.sh` | yes | 4 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_timeline_basic.service` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_timeline_begin.service` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_web_trust_check.py` | yes | 4 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/browser_workload_contract.py` | yes | 10 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/browser_composite_manifest.py, tools/riscv/debian/rootfs/browser_composite_capture.py, tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_composite_capture.py, tools/riscv/tests/test_browser_composite_manifest.py, tools/riscv/tests/test_browser_workload_contract.py` |  |  |
| `tools/riscv/debian/rootfs/build_rootfs.sh` | yes | 11 | 26 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_m5_runtime_gate.py, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_m5_network.py, tools/riscv/tests/test_debian_m6_browser.py, tools/riscv/tests/test_debian_m7_baidu.py, tools/riscv/tests/test_debian_m8_browser_quality.py, tools/riscv/tests/test_debian_m9_software.py` |  |  |
| `tools/riscv/debian/rootfs/build_stage1.sh` | yes | 1 | 20 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/contract.py` | yes | 168 | 295 | 保留 |
| ↳ 活动引用 |  | `Makefile, book/book/print.html, book/book/to-contribute/coding-guidelines/for-hardware/index.html, book/book/to-contribute/coding-guidelines/for-maintainability/design.html, book/book/to-contribute/coding-guidelines/for-maintainability/naming.html, book/book/to-contribute/coding-guidelines/index.html, test/initramfs/src/regression/scripts/run_regression_test.sh, tools/nixos/riscv_preflight.py` |  |  |
| `tools/riscv/debian/rootfs/debug_console_protocol.py` | yes | 5 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/megrez_board_session.py, tools/riscv/megrez_boot_stability.py, tools/riscv/megrez_physical_graphics.py, tools/riscv/tests/test_debian_debug_console.py` |  |  |
| `tools/riscv/debian/rootfs/debug_console_qemu_gate.py` | yes | 4 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/physical_graphics_qemu_gate.py, tools/riscv/tests/test_debian_debug_console.py, tools/riscv/tests/test_physical_graphics_qemu_gate.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_display_provider.sh` | yes | 5 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_drm.conf` | no | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_drm_libdrm_probe.py` | no | 3 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_desktop_drm_libdrm_probe.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_drm_x11_screenshot.py` | no | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_desktop_drm_x11_screenshot.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_fbdev_no_input.conf` | yes | 4 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_fbdev_qemu.conf` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/desktop_input_identity.py` | yes | 5 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_physical_graphics.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_lxpanel.conf` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m3_device_access.sh` | yes | 5 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m3_evidence.sh` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m3_gate.py` | yes | 12 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m4_gate.py, tools/riscv/debian/rootfs/desktop_m5_network_gate.py, tools/riscv/debian/rootfs/desktop_m6_browser_gate.py, tools/riscv/debian/rootfs/desktop_m7_baidu_gate.py, tools/riscv/debian/rootfs/firefox_startup_profile.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m3_session.sh` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_core_evidence.service` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_display_safe_gate.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_drm_gate.py` | no | 1 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_drm_recovery_gate.py` | no | 3 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/firefox_drm_recovery_gate.py, tools/riscv/tests/test_debian_desktop_m4_drm_recovery_gate.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_evidence.sh` | yes | 3 | 8 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_m5_network.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_framebuffer_probe.py` | yes | 3 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/desktop_m4_evidence.sh, tools/riscv/tests/test_debian_desktop_m4_framebuffer_probe.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_framebuffer_rw.py` | yes | 2 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_gate.py` | yes | 25 | 16 | 保留 |
| ↳ 活动引用 |  | `.tmp_diag2.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m4_display_safe_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_recovery_gate.py, tools/riscv/debian/rootfs/desktop_m4_recovery_gate.py, tools/riscv/debian/rootfs/desktop_m4_scanout_gate.py, tools/riscv/debian/rootfs/desktop_m4_x11_gate.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_recovery_gate.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_scanout_gate.py` | no | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_session.sh` | yes | 4 | 7 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_m5_network.py, tools/riscv/tests/test_debian_m6_browser.py, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_welcome.html` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_x11_gate.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_desktop_m4_x11_gate.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m4_x11_paint.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/desktop_m5_evidence.sh` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_browser_m5_runtime_gate.py, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_signed_sources.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh` | yes | 4 | 10 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_m5_network.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m5_network_gate.py` | yes | 20 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_contract.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m6_browser_gate.py, tools/riscv/debian/rootfs/desktop_m9_software_gate.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m5_overview.conf` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py` | yes | 12 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m6_browser_gate.py, tools/riscv/debian/rootfs/desktop_m9_software_gate.py, tools/riscv/megrez_single_packet_qemu_gate.py, tools/riscv/physical_graphics_qemu_gate.py, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m5_session.sh` | yes | 5 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_m5_runtime_gate.py, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_signed_sources.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m6_browser_evidence.sh` | yes | 2 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m6_browser.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m6_browser_gate.py` | yes | 9 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/desktop_m7_baidu_gate.py, tools/riscv/megrez_debug_contract.py, tools/riscv/megrez_debug_desktop.py, tools/riscv/megrez_gmac_gate.py, tools/riscv/tests/test_debian_m6_browser.py, tools/riscv/tests/test_debian_m7_baidu.py, tools/riscv/tests/test_megrez_debug.py, tools/riscv/tests/test_megrez_debug_desktop.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m6_javascript.html` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m6_browser.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m6_javascript_pass.html` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m6_browser.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m7_baidu_evidence.sh` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m7_baidu.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m7_baidu_gate.py` | yes | 7 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/megrez_debug_contract.py, tools/riscv/megrez_debug_desktop.py, tools/riscv/megrez_gmac_gate.py, tools/riscv/tests/test_debian_m7_baidu.py, tools/riscv/tests/test_megrez_debug.py, tools/riscv/tests/test_megrez_debug_desktop.py, tools/riscv/tests/test_megrez_gmac_gate.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m8_browser_quality.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m9_software_evidence.sh` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m9_software.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_m9_software_gate.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_m9_software.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_pcmanfm.conf` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/desktop_wallpaper.svg` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/dev_overlay.py` | yes | 4 | 19 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/diagnostics/firefox_dmesg_prepare.py, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_dev_overlay.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/README.md` | no | 29 | 91 | 保留 |
| ↳ 活动引用 |  | `Cargo.toml, book/book/kernel/index.html, book/book/print.html, book/book/to-contribute/coding-guidelines/for-documentation/general-style.html, book/book/to-contribute/coding-guidelines/for-documentation/index.html, book/book/to-contribute/coding-guidelines/index.html, osdk/Cargo.toml, osdk/deps/frame-allocator/Cargo.toml` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` | no | 0 | 4 | 候选删除，需人工确认动态引用 |
| `tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py` | no | 0 | 6 | 候选删除，需人工确认动态引用 |
| `tools/riscv/debian/rootfs/firefox_compat/package_provenance_gate.py` | no | 3 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh, tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py, tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py` | no | 1 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/drm/firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/source_package_gate.py` | no | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/source_package_manifest.json` | no | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/source_patch_gate.py` | no | 3 | 7 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh, tools/riscv/debian/rootfs/firefox_compat/package_provenance_gate.py, tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_compat/source_patch_manifest.json` | no | 4 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh, tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py, tools/riscv/debian/rootfs/firefox_compat/source_patch_gate.py, tools/riscv/tests/test_firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_diagnostic_snapshot.py` | yes | 4 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/diagnostics/firefox_dmesg_prepare.py, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | no | 0 | 1 | 候选删除，需人工确认动态引用 |
| `tools/riscv/debian/rootfs/firefox_drm_recovery_gate.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_desktop_m4_drm_recovery_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_drm_scene.html` | no | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_drm_scene.service` | no | 3 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_drm_scene_gate.py` | no | 5 | 22 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/firefox_marionette_probe_overlay.json, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/debian/rootfs/firefox_jit_overlay.py` | yes | 3 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_marionette_probe.service` | no | 2 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/firefox_marionette_probe_overlay.json, tools/riscv/tests/test_firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_marionette_probe_overlay.json` | no | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_firefox_drm_scene_gate.py` |  |  |
| `tools/riscv/debian/rootfs/firefox_startup_profile.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_firefox_debug_tool.py` |  |  |
| `tools/riscv/debian/rootfs/fsops.py` | yes | 4 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_debian_signed_sources.py` |  |  |
| `tools/riscv/debian/rootfs/gate_protocol.py` | yes | 28 | 11 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m3_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_recovery_gate.py, tools/riscv/debian/rootfs/desktop_m4_gate.py, tools/riscv/debian/rootfs/desktop_m4_recovery_gate.py, tools/riscv/debian/rootfs/desktop_m4_x11_gate.py` |  |  |
| `tools/riscv/debian/rootfs/gate_runtime.py` | yes | 45 | 7 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m3_gate.py, tools/riscv/debian/rootfs/desktop_m4_display_safe_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_recovery_gate.py, tools/riscv/debian/rootfs/desktop_m4_gate.py` |  |  |
| `tools/riscv/debian/rootfs/gmac_single_packet_evidence.py` | no | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/logind_diagnostic.service` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/logind_diagnostic.sh` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/megrez_clock_sync.py` | yes | 6 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_clock_sync.py` |  |  |
| `tools/riscv/debian/rootfs/megrez_dns_shim.py` | no | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh` |  |  |
| `tools/riscv/debian/rootfs/megrez_installer.py` | yes | 4 | 11 | 保留 |
| ↳ 活动引用 |  | `.tmp_desktop_m4_install.py, tools/riscv/megrez_debian_install.py, tools/riscv/megrez_debian_shell_cli_io.py, tools/riscv/tests/test_megrez_debian_installer.py` |  |  |
| `tools/riscv/debian/rootfs/megrez_safe_reboot.sh` | yes | 3 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_m5_network.py` |  |  |
| `tools/riscv/debian/rootfs/physical_external_services_quiesce.sh` | yes | 7 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_physical_graphics.py` |  |  |
| `tools/riscv/debian/rootfs/physical_graphics_control.sh` | yes | 6 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_physical_graphics.py` |  |  |
| `tools/riscv/debian/rootfs/physical_graphics_gate.py` | yes | 11 | 9 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/firefox_fast_check.sh, tools/riscv/megrez_firefox_browse.py, tools/riscv/megrez_physical_graphics.py, tools/riscv/tests/test_debian_browser_web.py` |  |  |
| `tools/riscv/debian/rootfs/physical_graphics_interaction.html` | yes | 6 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_web_dev_overlay.json, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_physical_graphics_gate.py` |  |  |
| `tools/riscv/debian/rootfs/physical_system_probe.sh` | yes | 3 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_debian_rootfs.py, tools/riscv/tests/test_megrez_physical_graphics.py` |  |  |
| `tools/riscv/debian/rootfs/profiles.py` | yes | 51 | 49 | 保留 |
| ↳ 活动引用 |  | `book/book/osdk/reference/manifest.html, book/book/print.html, tools/riscv/debian/rootfs/browser_web_evidence.sh, tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/contract.py, tools/riscv/debian/rootfs/desktop_m4_evidence.sh, tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh, tools/riscv/debian/rootfs/gate_protocol.py` |  |  |
| `tools/riscv/debian/rootfs/rootfs_gate.py` | yes | 34 | 13 | 保留 |
| ↳ 活动引用 |  | `.tmp_diag2.py, Makefile, tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_contract.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m3_gate.py, tools/riscv/debian/rootfs/desktop_m4_display_safe_gate.py` |  |  |
| `tools/riscv/debian/rootfs/rootfs_gate_backend.py` | yes | 22 | 6 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m3_gate.py, tools/riscv/debian/rootfs/desktop_m4_display_safe_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_recovery_gate.py, tools/riscv/debian/rootfs/desktop_m4_gate.py` |  |  |
| `tools/riscv/debian/rootfs/signed_sources.py` | yes | 6 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/debian/rootfs/contract.py, tools/riscv/megrez_debug.py, tools/riscv/tests/test_debian_browser_web.py, tools/riscv/tests/test_debian_signed_sources.py, tools/riscv/tests/test_megrez_debug.py` |  |  |
| `tools/riscv/debian/rootfs/stage1_basic.py` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_stage1.sh, tools/riscv/tests/test_megrez_boot_menu.py` |  |  |
| `tools/riscv/debian/rootfs/systemd_m2_evidence.sh` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/debian/rootfs/build_rootfs.sh, tools/riscv/tests/test_debian_rootfs.py` |  |  |
| `tools/riscv/debian/rootfs/systemd_m2_gate.py` | yes | 21 | 6 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/debian/rootfs/browser_m5_qemu_gate.py, tools/riscv/debian/rootfs/browser_web_qemu_gate.py, tools/riscv/debian/rootfs/debug_console_qemu_gate.py, tools/riscv/debian/rootfs/desktop_m3_gate.py, tools/riscv/debian/rootfs/desktop_m4_display_safe_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_gate.py, tools/riscv/debian/rootfs/desktop_m4_drm_recovery_gate.py` |  |  |
| `tools/riscv/tests/test_atomic_mode_diagnostics.py` | no | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_browser_composite_capture.py` | yes | 2 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py` |  |  |
| `tools/riscv/tests/test_browser_composite_manifest.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_browser_daily_use_contract.py` | yes | 6 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py, tools/riscv/tests/test_browser_daily_use_upload.py, tools/riscv/tests/test_megrez_firefox_daily_use.py, tools/riscv/tests/test_megrez_firefox_daily_use_report.py` |  |  |
| `tools/riscv/tests/test_browser_daily_use_gate.py` | yes | 3 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/tests/test_browser_daily_use_upload.py` | yes | 3 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/tests/test_browser_interaction_perf.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_browser_perf_capture.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_browser_daily_use_gate.py` |  |  |
| `tools/riscv/tests/test_browser_performance_provenance.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_browser_system_time.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_browser_workload_contract.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_debian_browser_m5.py` | yes | 2 | 2 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py` |  |  |
| `tools/riscv/tests/test_debian_browser_m5_runtime_gate.py` | yes | 2 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_browser_daily_use_gate.py` |  |  |
| `tools/riscv/tests/test_debian_browser_web.py` | yes | 1 | 11 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_debian_debug_console.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_debian_desktop_m4_drm_recovery_gate.py` | no | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_debian_desktop_m4_framebuffer_probe.py` | yes | 0 | 4 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_debian_desktop_m4_x11_gate.py` | yes | 0 | 4 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_debian_dev_overlay.py` | yes | 2 | 13 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_debian_m5_network.py` | yes | 0 | 10 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_debian_m6_browser.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/tests/test_debian_m7_baidu.py` |  |  |
| `tools/riscv/tests/test_debian_m7_baidu.py` | yes | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_debian_m8_browser_quality.py` | yes | 0 | 3 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_debian_m9_software.py` | yes | 0 | 3 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_debian_rootfs.py` | yes | 2 | 45 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/tests/test_debian_signed_sources.py` | yes | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_desktop_drm_libdrm_probe.py` | no | 0 | 3 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_desktop_drm_x11_screenshot.py` | no | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_drm_cursor_gate.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_drm_gem_gate.py` | no | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_drm_render_node_gate.py` | no | 1 | 6 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_drm_scanout_fault_gate.py` | no | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_drm_scanout_gate.py` | no | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_drm_virgl_host_preflight.py` | no | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_drm_virgl_param_gate.py` | no | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_drm_xorg_modesetting_gate.py` | no | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_dual_host_qemu_probe.py` | yes | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_dwmac_rx_liveness_model.py` | yes | 1 | 10 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_eic7700_platform_contract.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_firefox_debug_tool.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_firefox_drm_scene_gate.py` | no | 1 | 21 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_firefox_webrender_gate.py` | no | 1 | 7 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_first_process_diag_source.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_linux_guest_stage1_exec.py` | yes | 0 | 1 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_log_profile.py` | yes | 0 | 1 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_ltp_gate.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_ltp_guest_runner.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_ltp_manifest.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_ltp_package.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_ltp_result.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_ltp_suite.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_board_session.py` | yes | 1 | 15 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_boot_manifest.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_boot_menu.py` | yes | 1 | 9 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_boot_stability.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_clock_sync.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_control_chain.py` | no | 1 | 6 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_debian_installer.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_debian_shell.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_debug.py` | yes | 1 | 8 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_debug_desktop.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_desktop.py` | yes | 1 | 7 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_desktop_boot.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_display_gate.py` | yes | 1 | 6 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_firefox_browse.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_firefox_daily_use.py` | yes | 2 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh` |  |  |
| `tools/riscv/tests/test_megrez_firefox_daily_use_report.py` | yes | 3 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/tests/test_megrez_gmac_contract.py` | yes | 1 | 5 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_gmac_gate.py` | yes | 1 | 13 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_hardware_probe.py` | yes | 1 | 8 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_install_workflow.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_network_fixture.py` | yes | 3 | 6 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/tests/test_megrez_patch_dtb.py` | yes | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_megrez_physical_graphics.py` | yes | 3 | 9 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_megrez_firefox_daily_use.py` |  |  |
| `tools/riscv/tests/test_megrez_preboard.py` | yes | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_probe.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_proxy_bridge.py` | yes | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_recovery_epoch.py` | no | 1 | 4 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_rockos_attestation.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_safety.py` | no | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_megrez_xmodem.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_nixos_m7_assets.py` | yes | 2 | 3 | 保留 |
| ↳ 活动引用 |  | `tools/riscv/nixos/track-admission-overrides.v1.json, tools/riscv/nixos/track-admission.v1.json` |  |  |
| `tools/riscv/tests/test_nixos_riscv_preflight.py` | yes | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_nixos_track_audit.py` | yes | 0 | 4 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_physical_graphics_gate.py` | yes | 3 | 7 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/firefox_fast_check.sh, tools/riscv/tests/test_megrez_physical_graphics.py` |  |  |
| `tools/riscv/tests/test_physical_graphics_qemu_gate.py` | yes | 2 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile, tools/riscv/tests/test_megrez_physical_graphics.py` |  |  |
| `tools/riscv/tests/test_qemu_dwc3_contract.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_fallback_receipt.py` | no | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_gmac_contract.py` | no | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_recovery_faults.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_scanout_contract.py` | no | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_tcg_plugin.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_uboot_booti.py` | yes | 1 | 8 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_uboot_contracts.py` | yes | 1 | 11 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_qemu_uboot_matrix.py` | no | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_refactor_reference_scan.py` | yes | 0 | 3 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_renode_scenario.py` | no | 1 | 1 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_riscv_ipi_ack_model.py` | yes | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_riscv_rootfs_builder_image.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_scheduler_irq_level_contract.py` | no | 0 | 3 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_syscall_diagnostics.py` | yes | 0 | 1 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_tlb_shootdown_probe_timing.py` | yes | 0 | 1 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_validate_run_kernel_log.py` | yes | 1 | 2 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
| `tools/riscv/tests/test_virtio_network_init_contract.py` | no | 0 | 2 | 候选删除，需人工确认动态引用 |
| `tools/riscv/tests/test_xhci_input_gate.py` | yes | 1 | 3 | 保留 |
| ↳ 活动引用 |  | `Makefile` |  |  |
