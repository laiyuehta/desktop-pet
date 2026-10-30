use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::time::Duration;

use serde_json::{json, Value};

pub const SERVICE_DOWN_MSG: &str = "Wi‑Fi 服務未啟動";

#[cfg(unix)]
fn connect_stream(
    socket_path: &Path,
    timeout: Duration,
) -> std::io::Result<(
    BufReader<std::os::unix::net::UnixStream>,
    std::os::unix::net::UnixStream,
)> {
    use std::os::unix::net::UnixStream;
    let stream = UnixStream::connect(socket_path)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let writer = stream.try_clone()?;
    Ok((BufReader::new(stream), writer))
}

#[cfg(not(unix))]
fn connect_stream(
    socket_path: &Path,
    timeout: Duration,
) -> std::io::Result<(BufReader<std::net::TcpStream>, std::net::TcpStream)> {
    use std::net::{SocketAddr, TcpStream};
    let addr_str = std::fs::read_to_string(socket_path)?;
    let addr: SocketAddr = addr_str
        .trim()
        .parse()
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    let stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let writer = stream.try_clone()?;
    Ok((BufReader::new(stream), writer))
}

pub fn request(socket_path: &Path, req: &Value, timeout: Duration) -> Result<Value, String> {
    let (mut reader, mut writer) =
        connect_stream(socket_path, timeout).map_err(|e| format!("{SERVICE_DOWN_MSG}（{e}）"))?;
    let line = serde_json::to_string(req).map_err(|e| e.to_string())?;
    writeln!(writer, "{line}").map_err(|e| format!("傳送請求失敗：{e}"))?;
    writer.flush().map_err(|e| format!("傳送請求失敗：{e}"))?;

    let mut resp_line = String::new();
    let n = reader
        .read_line(&mut resp_line)
        .map_err(|e| format!("讀取回應失敗：{e}"))?;
    if n == 0 {
        return Err("piwifi 中斷連線".to_string());
    }
    serde_json::from_str(&resp_line).map_err(|e| format!("回應不是合法 JSON：{e}"))
}

pub fn subscribe_events<F>(
    socket_path: &Path,
    read_timeout: Option<Duration>,
    mut on_event: F,
) -> Result<(), String>
where
    F: FnMut(Value) -> bool,
{
    let timeout = read_timeout.unwrap_or(Duration::from_secs(3600));
    let (mut reader, mut writer) =
        connect_stream(socket_path, timeout).map_err(|e| format!("{SERVICE_DOWN_MSG}（{e}）"))?;
    let sub_req = json!({"v": 1, "cmd": "subscribe"});
    writeln!(writer, "{sub_req}").map_err(|e| format!("傳送訂閱失敗：{e}"))?;
    writer.flush().map_err(|e| format!("傳送訂閱失敗：{e}"))?;

    let mut ack_line = String::new();
    let n = reader
        .read_line(&mut ack_line)
        .map_err(|e| format!("讀取訂閱回應失敗：{e}"))?;
    if n == 0 {
        return Err("piwifi 中斷連線".to_string());
    }
    let ack: Value =
        serde_json::from_str(&ack_line).map_err(|e| format!("回應不是合法 JSON：{e}"))?;
    if ack.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(ack
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("訂閱失敗")
            .to_string());
    }

    let mut line = String::new();
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                if let Ok(val) = serde_json::from_str::<Value>(trimmed) {
                    if !on_event(val) {
                        break;
                    }
                }
            }
            Err(e) => return Err(format!("讀取事件中斷：{e}")),
        }
    }
    Ok(())
}
