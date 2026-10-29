use std::fs;
use std::path::Path;
use std::sync::Arc;

use serde_json::{json, Value};

use crate::manager::{ManagerError, WifiManager};
use crate::nm::result_text;

pub const PROTO_VERSION: u32 = 1;

pub enum DispatchResult {
    Response(Value),
    Subscribe,
}

pub fn error_json(kind: &str, error: &str) -> Value {
    json!({
        "ok": false,
        "proto": PROTO_VERSION,
        "kind": kind,
        "error": error,
    })
}

pub fn manager_error_json(err: &ManagerError) -> Value {
    match err {
        ManagerError::Observe => json!({
            "ok": false,
            "proto": PROTO_VERSION,
            "kind": "observe_mode",
            "error": "observe 模式",
        }),
        ManagerError::PinRequired => error_json("pin_required", "需要 PIN"),
        ManagerError::Validation(msg) => error_json("bad_request", msg),
        ManagerError::Nm(nm_err) => error_json(&nm_err.kind, result_text(&nm_err.kind)),
    }
}

pub fn handle_line(mgr: &Arc<WifiManager>, line: &str) -> DispatchResult {
    let parsed: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => {
            return DispatchResult::Response(error_json(
                "bad_request",
                &format!("看不懂的請求：{e}"),
            ))
        }
    };
    let Some(obj) = parsed.as_object() else {
        return DispatchResult::Response(error_json("bad_request", "要 JSON 物件"));
    };

    // 檢查協定版本：帶 "v":1，沒帶視為 1
    if let Some(v_val) = obj.get("v") {
        if v_val.as_u64() != Some(u64::from(PROTO_VERSION)) {
            return DispatchResult::Response(error_json("unsupported_version", "不支援的協定版本"));
        }
    }

    let pin = obj.get("pin").and_then(Value::as_str);
    if let Err(e) = mgr.authorize(pin) {
        return DispatchResult::Response(manager_error_json(&e));
    }

    let Some(cmd) = obj.get("cmd").and_then(Value::as_str) else {
        return DispatchResult::Response(error_json("bad_request", "缺少 cmd 欄位"));
    };

    match cmd {
        "status" => {
            let cache_s = obj.get("cache_s").and_then(Value::as_f64).unwrap_or(1.5);
            let st = mgr.status(cache_s);
            let val = serde_json::to_value(st).unwrap_or_else(|_| json!({"ok": false}));
            DispatchResult::Response(val)
        }
        "scan" => {
            let rescan = obj.get("rescan").and_then(Value::as_bool).unwrap_or(true);
            match mgr.scan(rescan) {
                Ok(networks) => DispatchResult::Response(json!({
                    "ok": true,
                    "proto": PROTO_VERSION,
                    "networks": networks,
                })),
                Err(e) => DispatchResult::Response(manager_error_json(&ManagerError::Nm(e))),
            }
        }
        "saved" => match mgr.saved() {
            Ok(saved) => DispatchResult::Response(json!({
                "ok": true,
                "proto": PROTO_VERSION,
                "saved": saved,
            })),
            Err(e) => DispatchResult::Response(manager_error_json(&ManagerError::Nm(e))),
        },
        "connect" => {
            let ssid = obj
                .get("ssid")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let password = obj
                .get("password")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let hidden = obj.get("hidden").and_then(Value::as_bool).unwrap_or(false);
            let uuid = obj
                .get("uuid")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let wait = obj.get("wait").and_then(Value::as_bool).unwrap_or(false);
            match mgr.start_connect(ssid, password, hidden, uuid, !wait) {
                Ok(op) => DispatchResult::Response(json!({
                    "ok": true,
                    "proto": PROTO_VERSION,
                    "op": op,
                })),
                Err(e) => DispatchResult::Response(manager_error_json(&e)),
            }
        }
        "forget" => {
            let uuid = obj.get("uuid").and_then(Value::as_str).unwrap_or("");
            match mgr.forget(uuid) {
                Ok(ssid) => DispatchResult::Response(json!({
                    "ok": true,
                    "proto": PROTO_VERSION,
                    "ssid": ssid,
                })),
                Err(e) => DispatchResult::Response(manager_error_json(&e)),
            }
        }
        "priority" => {
            let order = obj.get("order").and_then(Value::as_array).map(|arr| {
                arr.iter()
                    .filter_map(Value::as_str)
                    .map(ToString::to_string)
                    .collect::<Vec<String>>()
            });
            let uuid = obj
                .get("uuid")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let direction = obj
                .get("direction")
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let value = obj.get("value").and_then(Value::as_i64).map(|v| v as i32);
            match mgr.set_priority(order, uuid, direction, value) {
                Ok(saved) => DispatchResult::Response(json!({
                    "ok": true,
                    "proto": PROTO_VERSION,
                    "saved": saved,
                })),
                Err(e) => DispatchResult::Response(manager_error_json(&e)),
            }
        }
        "hotspot" => {
            let Some(on) = obj.get("on").and_then(Value::as_bool) else {
                return DispatchResult::Response(error_json("bad_request", "需要布林欄位 on"));
            };
            match mgr.set_hotspot(on) {
                Ok((true, mode)) => DispatchResult::Response(json!({
                    "ok": true,
                    "proto": PROTO_VERSION,
                    "mode": mode,
                })),
                Ok((false, mode)) => DispatchResult::Response(json!({
                    "ok": false,
                    "proto": PROTO_VERSION,
                    "mode": mode,
                    "kind": "failed",
                    "error": "開熱點失敗（沒有熱點設定檔或網卡不支援）",
                })),
                Err(e) => DispatchResult::Response(manager_error_json(&e)),
            }
        }
        "subscribe" => DispatchResult::Subscribe,
        other => {
            DispatchResult::Response(error_json("bad_request", &format!("未知的 cmd：{other}")))
        }
    }
}

pub fn write_status_file(mgr: &WifiManager, path: &Path) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = fs::create_dir_all(parent);
        }
    }
    let mut st = mgr.status(0.0);
    st.ok = None;
    let body = serde_json::to_string(&st).map_err(std::io::Error::other)?;
    let tmp_path = path.with_extension("json.tmp");
    fs::write(&tmp_path, body)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&tmp_path, fs::Permissions::from_mode(0o644));
    }
    fs::rename(&tmp_path, path)?;
    Ok(())
}
