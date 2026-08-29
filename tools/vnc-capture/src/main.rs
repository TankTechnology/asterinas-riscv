// SPDX-License-Identifier: MPL-2.0

//! vnc-capture — Capture QEMU VNC framebuffer via monitor socket.
//!
//! Connects to a QEMU monitor Unix socket, issues `screendump` to capture the
//! guest framebuffer as a PPM, then converts it to PNG. Also supports sending
//! keystrokes via `sendkey` for automated input testing.
//!
//! Usage:
//!   vnc-capture screenshot <monitor-socket> <output-png>
//!   vnc-capture sendkey  <monitor-socket> <key> [<key>...]

use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    time::Duration,
};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage:");
        eprintln!("  vnc-capture screenshot <monitor-socket> <output-png>");
        eprintln!("  vnc-capture sendkey  <monitor-socket> <key> [<key>...]");
        std::process::exit(1);
    }

    match args[1].as_str() {
        "screenshot" => {
            if args.len() != 4 {
                anyhow::bail!("Usage: vnc-capture screenshot <monitor-socket> <output-png>");
            }
            cmd_screenshot(&args[2], &args[3])?;
        }
        "sendkey" => {
            if args.len() < 4 {
                anyhow::bail!("Usage: vnc-capture sendkey <monitor-socket> <key> [<key>...]");
            }
            cmd_sendkey(&args[2], &args[3..])?;
        }
        _ => {
            anyhow::bail!("Unknown command: {}", args[1]);
        }
    }
    Ok(())
}

/// Connect to a QEMU monitor Unix socket, draining the welcome banner.
fn mon_connect(sock: &str) -> anyhow::Result<UnixStream> {
    let mut stream = UnixStream::connect(sock)?;
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    // Drain the QEMU monitor welcome banner (QEMU X.Y.Z monitor - type 'help'...)
    std::thread::sleep(Duration::from_millis(200));
    let mut buf = [0u8; 4096];
    let _ = stream.read(&mut buf);
    Ok(stream)
}

fn mon_cmd(stream: &mut UnixStream, cmd: &str) -> anyhow::Result<()> {
    stream.write_all(cmd.as_bytes())?;
    stream.write_all(b"\n")?;
    stream.flush()?;
    std::thread::sleep(Duration::from_millis(600));
    Ok(())
}

fn cmd_screenshot(mon_sock: &str, output_png: &str) -> anyhow::Result<()> {
    let tmp_ppm = format!("/tmp/vnc-capture-{}.ppm", std::process::id());
    let mut stream = mon_connect(mon_sock)?;
    mon_cmd(&mut stream, &format!("screendump {}", tmp_ppm))?;
    // screendump is async inside QEMU; give it time to write the file.
    std::thread::sleep(Duration::from_secs(2));

    let img = image::open(&tmp_ppm)?;
    img.save(output_png)?;
    std::fs::remove_file(&tmp_ppm).ok();
    println!("Captured: {}", output_png);
    Ok(())
}

fn cmd_sendkey(mon_sock: &str, keys: &[String]) -> anyhow::Result<()> {
    let mut stream = mon_connect(mon_sock)?;
    let key_list = keys.join("-");
    mon_cmd(&mut stream, &format!("sendkey {}", key_list))?;
    println!("Sent keys: {}", key_list);
    Ok(())
}
