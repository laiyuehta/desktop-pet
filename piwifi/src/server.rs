use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::json;

use crate::log_info;
use crate::manager::WifiManager;
use crate::protocol::{error_json, handle_line, write_status_file, DispatchResult, PROTO_VERSION};

pub const DEFAULT_SOCKET_PATH: &str = "/run/piwifi/piwifi.sock";
pub const DEFAULT_STATUS_PATH: &str = "/run/piwifi/status.json";

pub fn serve_client_stream<R: BufRead, W: Write>(
    mgr: Arc<WifiManager>,
    mut reader: R,
    mut writer: W,
    stop: Arc<AtomicBool>,
) {
    mgr.client_connected();
    let mut line = String::new();
    while !stop.load(Ordering::Relaxed) {
        line.clear();
        let n = match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(n) => n,
            Err(_) => break,
        };
        if n > 8192 {
            let err = error_json("bad_request", "請求太長");
            let _ = writeln!(writer, "{err}");
            let _ = writer.flush();
            break;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        match handle_line(&mgr, trimmed) {
            DispatchResult::Response(val) => {
                if writeln!(writer, "{val}").is_err() || writer.flush().is_err() {
                    break;
                }
            }
            DispatchResult::Subscribe => {
                let rx = mgr.subscribe();
                let ack = json!({
                    "ok": true,
                    "proto": PROTO_VERSION,
                    "version": env!("CARGO_PKG_VERSION"),
                });
                if writeln!(writer, "{ack}").is_err() || writer.flush().is_err() {
                    break;
                }
                while !stop.load(Ordering::Relaxed) {
                    match rx.recv_timeout(Duration::from_millis(200)) {
                        Ok(ev) => {
                            let Ok(s) = serde_json::to_string(&ev) else {
                                continue;
                            };
                            if writeln!(writer, "{s}").is_err() || writer.flush().is_err() {
                                break;
                            }
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
                        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                    }
                }
                break;
            }
        }
    }
    mgr.client_disconnected();
}

pub struct ServerHandle {
    pub socket_path: PathBuf,
    pub status_path: Option<PathBuf>,
    pub stop: Arc<AtomicBool>,
    threads: Vec<JoinHandle<()>>,
}

impl ServerHandle {
    pub fn start(
        mgr: Arc<WifiManager>,
        socket_path: impl AsRef<Path>,
        status_path: Option<PathBuf>,
        group: Option<String>,
        tick_interval: Option<Duration>,
    ) -> std::io::Result<Self> {
        let sock_path = socket_path.as_ref().to_path_buf();
        if let Some(parent) = sock_path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        if sock_path.exists() {
            let _ = fs::remove_file(&sock_path);
        }

        let stop = Arc::new(AtomicBool::new(false));
        let mut threads = Vec::new();

        if let Some(ref st_path) = status_path {
            let _ = write_status_file(&mgr, st_path);
            let mgr_st = Arc::clone(&mgr);
            let st_path_clone = st_path.clone();
            let stop_st = Arc::clone(&stop);
            threads.push(thread::spawn(move || {
                while !stop_st.load(Ordering::Relaxed) {
                    let _ = write_status_file(&mgr_st, &st_path_clone);
                    for _ in 0..10 {
                        if stop_st.load(Ordering::Relaxed) {
                            break;
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                }
                let _ = write_status_file(&mgr_st, &st_path_clone);
            }));
        }

        if let Some(interval) = tick_interval {
            let mgr_tick = Arc::clone(&mgr);
            let stop_tick = Arc::clone(&stop);
            let st_path_tick = status_path.clone();
            threads.push(thread::spawn(move || {
                while !stop_tick.load(Ordering::Relaxed) {
                    mgr_tick.tick();
                    if let Some(ref p) = st_path_tick {
                        let _ = write_status_file(&mgr_tick, p);
                    }
                    let steps = (interval.as_millis() / 100).max(1);
                    for _ in 0..steps {
                        if stop_tick.load(Ordering::Relaxed) {
                            break;
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                }
            }));
        }

        #[cfg(unix)]
        {
            use crate::log_warn;
            use std::os::unix::fs::PermissionsExt;
            use std::os::unix::net::UnixListener;

            let listener = UnixListener::bind(&sock_path)?;
            listener.set_nonblocking(true)?;
            let _ = fs::set_permissions(&sock_path, fs::Permissions::from_mode(0o660));
            if let Some(ref grp) = group {
                if !grp.is_empty() {
                    let status = std::process::Command::new("chgrp")
                        .arg(grp)
                        .arg(&sock_path)
                        .status();
                    if let Err(e) = status {
                        log_warn!("socket 群組設不了 {}：{}", grp, e);
                    }
                }
            }
            log_info!("piwifi 在 {} 等 client", sock_path.display());

            let mgr_acc = Arc::clone(&mgr);
            let stop_acc = Arc::clone(&stop);
            threads.push(thread::spawn(move || {
                while !stop_acc.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            let _ = stream.set_nonblocking(false);
                            let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
                            let _ = stream.set_write_timeout(Some(Duration::from_secs(30)));
                            let Ok(writer) = stream.try_clone() else {
                                continue;
                            };
                            let reader = BufReader::new(stream);
                            let m = Arc::clone(&mgr_acc);
                            let s = Arc::clone(&stop_acc);
                            thread::spawn(move || {
                                serve_client_stream(m, reader, writer, s);
                            });
                        }
                        Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(25));
                        }
                        Err(_) => {
                            if stop_acc.load(Ordering::Relaxed) {
                                break;
                            }
                            thread::sleep(Duration::from_millis(100));
                        }
                    }
                }
            }));
        }

        #[cfg(not(unix))]
        {
            use std::net::TcpListener;
            let _ = group;
            let listener = TcpListener::bind("127.0.0.1:0")?;
            let addr = listener.local_addr()?;
            fs::write(&sock_path, addr.to_string())?;
            listener.set_nonblocking(true)?;
            log_info!("piwifi 在 {} ({}) 等 client", sock_path.display(), addr);

            let mgr_acc = Arc::clone(&mgr);
            let stop_acc = Arc::clone(&stop);
            threads.push(thread::spawn(move || {
                while !stop_acc.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            let _ = stream.set_nonblocking(false);
                            let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
                            let _ = stream.set_write_timeout(Some(Duration::from_secs(30)));
                            let Ok(writer) = stream.try_clone() else {
                                continue;
                            };
                            let reader = BufReader::new(stream);
                            let m = Arc::clone(&mgr_acc);
                            let s = Arc::clone(&stop_acc);
                            thread::spawn(move || {
                                serve_client_stream(m, reader, writer, s);
                            });
                        }
                        Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(25));
                        }
                        Err(_) => {
                            if stop_acc.load(Ordering::Relaxed) {
                                break;
                            }
                            thread::sleep(Duration::from_millis(100));
                        }
                    }
                }
            }));
        }

        Ok(Self {
            socket_path: sock_path,
            status_path,
            stop,
            threads,
        })
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        for handle in self.threads.drain(..) {
            let _ = handle.join();
        }
        let _ = fs::remove_file(&self.socket_path);
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        self.stop();
    }
}
