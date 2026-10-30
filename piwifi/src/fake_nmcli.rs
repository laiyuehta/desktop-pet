use std::fs;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::nm::{escape_terse, CommandRunner, WIFI_TYPE};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct FakeConn {
    pub name: String,
    pub uuid: String,
    pub conn_type: String,
    pub priority: i32,
    pub timestamp: u64,
    pub ssid: Option<String>,
    pub psk: Option<String>,
    pub hidden: bool,
    pub mode: String,
    pub autoconnect: bool,
    pub key_mgmt: Option<String>,
    pub pmf: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct FakeAir {
    pub ssid: String,
    pub password: Option<String>,
    pub security: String,
    pub signal: i32,
    pub hidden: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FakeState {
    pub conns: Vec<FakeConn>,
    pub air: Vec<FakeAir>,
    pub active: Option<String>,
    pub n: u64,
    pub dev_state: String,
    pub ap_ok: bool,
    pub flaky_ap_once: bool,
    pub calls: Vec<Vec<String>>,
    pub raw_cli_calls: Vec<Vec<String>>,
}

impl Default for FakeState {
    fn default() -> Self {
        let mut st = Self {
            conns: Vec::new(),
            air: Vec::new(),
            active: None,
            n: 0,
            dev_state: "disconnected".to_string(),
            ap_ok: true,
            flaky_ap_once: false,
            calls: Vec::new(),
            raw_cli_calls: Vec::new(),
        };
        st.add_conn(
            "Wired connection 1",
            None,
            "802-3-ethernet",
            0,
            0,
            None,
            false,
            "infrastructure",
            true,
        );
        st
    }
}

impl FakeState {
    #[allow(clippy::too_many_arguments)]
    pub fn add_conn(
        &mut self,
        name: &str,
        ssid: Option<&str>,
        conn_type: &str,
        priority: i32,
        timestamp: u64,
        password: Option<&str>,
        hidden: bool,
        mode: &str,
        autoconnect: bool,
    ) -> String {
        self.n += 1;
        let uuid = format!("00000000-0000-0000-0000-{:012}", self.n);
        self.conns.push(FakeConn {
            name: name.to_string(),
            uuid: uuid.clone(),
            conn_type: conn_type.to_string(),
            priority,
            timestamp,
            ssid: ssid.map(ToString::to_string),
            psk: password.map(ToString::to_string),
            hidden,
            mode: mode.to_string(),
            autoconnect,
            key_mgmt: password.map(|_| "wpa-psk".to_string()),
            pmf: None,
        });
        uuid
    }

    pub fn set_air(
        &mut self,
        ssid: &str,
        password: Option<&str>,
        security: &str,
        signal: i32,
        hidden: bool,
    ) {
        if let Some(existing) = self.air.iter_mut().find(|a| a.ssid == ssid) {
            existing.password = password.map(ToString::to_string);
            existing.security = security.to_string();
            existing.signal = signal;
            existing.hidden = hidden;
        } else {
            self.air.push(FakeAir {
                ssid: ssid.to_string(),
                password: password.map(ToString::to_string),
                security: security.to_string(),
                signal,
                hidden,
            });
        }
    }

    pub fn remove_air(&mut self, ssid: &str) {
        self.air.retain(|a| a.ssid != ssid);
    }

    pub fn get_conn(&self, uuid: &str) -> Option<&FakeConn> {
        self.conns.iter().find(|c| c.uuid == uuid)
    }

    pub fn get_conn_mut(&mut self, uuid: &str) -> Option<&mut FakeConn> {
        self.conns.iter_mut().find(|c| c.uuid == uuid)
    }

    /// 判斷是否為會修改網路狀態的 nmcli 指令。
    pub fn is_mutating_cli(args: &[String]) -> bool {
        let mut iter = args.iter().map(String::as_str);
        while let Some(a) = iter.next() {
            if a == "connection" {
                if let Some(sub) = iter.next() {
                    return matches!(sub, "add" | "edit" | "up" | "down" | "delete" | "modify");
                }
            }
        }
        false
    }

    pub fn eval_cli(&mut self, args: &[String], input_text: Option<&str>) -> (i32, String, String) {
        self.raw_cli_calls.push(args.to_vec());
        let strs: Vec<&str> = args.iter().map(String::as_str).collect();

        // 1. -t -f NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,TIMESTAMP,ACTIVE,DEVICE connection show
        if strs.len() == 5
            && strs[0] == "-t"
            && strs[1] == "-f"
            && strs[3] == "connection"
            && strs[4] == "show"
        {
            let mut out = String::new();
            for c in &self.conns {
                let is_active =
                    self.active.as_deref() == Some(c.uuid.as_str()) || c.conn_type != WIFI_TYPE;
                let dev = if self.active.as_deref() == Some(c.uuid.as_str()) {
                    "wlan0"
                } else if c.conn_type != WIFI_TYPE {
                    "eth0"
                } else {
                    ""
                };
                out.push_str(&format!(
                    "{}:{}:{}:{}:{}:{}:{}\n",
                    escape_terse(&c.name),
                    escape_terse(&c.uuid),
                    escape_terse(&c.conn_type),
                    c.priority,
                    c.timestamp,
                    if is_active { "yes" } else { "no" },
                    dev
                ));
            }
            return (0, out, String::new());
        }

        // 2. -t -f 802-11-wireless... connection show <uuid>
        if strs.len() == 6
            && strs[0] == "-t"
            && strs[1] == "-f"
            && strs[3] == "connection"
            && strs[4] == "show"
        {
            let uuid = strs[5];
            let Some(c) = self.get_conn(uuid) else {
                return (
                    10,
                    String::new(),
                    format!("Error: {} - no such connection profile.", uuid),
                );
            };
            let ssid = escape_terse(c.ssid.as_deref().unwrap_or(""));
            let mode = escape_terse(&c.mode);
            let hidden = if c.hidden { "yes" } else { "no" };
            let km = escape_terse(c.key_mgmt.as_deref().unwrap_or(""));
            let out = format!(
                "802-11-wireless.ssid:{ssid}\n802-11-wireless.mode:{mode}\n802-11-wireless.hidden:{hidden}\n802-11-wireless-security.key-mgmt:{km}\n"
            );
            return (0, out, String::new());
        }

        // 3. -t -f DEVICE,TYPE,STATE,CONNECTION device status
        if strs.len() == 5
            && strs[0] == "-t"
            && strs[1] == "-f"
            && strs[3] == "device"
            && strs[4] == "status"
        {
            let wlan_state = if self.active.is_some() {
                "connected"
            } else {
                self.dev_state.as_str()
            };
            let wlan_conn = self
                .active
                .as_deref()
                .and_then(|u| self.get_conn(u))
                .map(|c| c.name.as_str())
                .unwrap_or("--");
            let out = format!(
                "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:{}:{}\n",
                escape_terse(wlan_state),
                escape_terse(wlan_conn)
            );
            return (0, out, String::new());
        }

        // 4. -t -f IP4.ADDRESS device show <dev>
        if strs.len() == 6
            && strs[0] == "-t"
            && strs[1] == "-f"
            && strs[2] == "IP4.ADDRESS"
            && strs[3] == "device"
            && strs[4] == "show"
        {
            let dev = strs[5];
            let ip = if dev == "eth0" {
                Some("192.0.2.43/24")
            } else if dev == "wlan0" {
                if let Some(act_uuid) = self.active.as_deref() {
                    if let Some(c) = self.get_conn(act_uuid) {
                        if c.mode == "ap" {
                            Some("192.0.2.1/24")
                        } else {
                            Some("192.0.2.101/24")
                        }
                    } else {
                        None
                    }
                } else {
                    None
                }
            } else {
                None
            };
            let out = match ip {
                Some(addr) => format!("IP4.ADDRESS[1]:{addr}\n"),
                None => String::new(),
            };
            return (0, out, String::new());
        }

        // 5. -t -f IN-USE,SSID,SIGNAL,SECURITY,CHAN device wifi list ifname <iface> --rescan <yes|no>
        if strs.len() >= 10
            && strs[0] == "-t"
            && strs[3] == "device"
            && strs[4] == "wifi"
            && strs[5] == "list"
        {
            let rescan = strs.last().copied().unwrap_or("no") == "yes";
            self.calls
                .push(vec!["list".to_string(), rescan.to_string()]);
            let cur_ssid = self
                .active
                .as_deref()
                .and_then(|u| self.get_conn(u))
                .and_then(|c| c.ssid.as_deref())
                .unwrap_or("");
            let mut out = String::new();
            for a in &self.air {
                let in_use = if !cur_ssid.is_empty() && a.ssid == cur_ssid {
                    "*"
                } else {
                    " "
                };
                let visible_ssid = if a.hidden {
                    String::new()
                } else {
                    escape_terse(&a.ssid)
                };
                out.push_str(&format!(
                    "{}:{}:{}:{}:1\n",
                    in_use,
                    visible_ssid,
                    a.signal,
                    escape_terse(&a.security)
                ));
            }
            return (0, out, String::new());
        }

        // 6. connection add ...
        if strs.len() >= 2 && strs[0] == "connection" && strs[1] == "add" {
            let mut name = "";
            let mut ssid = "";
            let mut hidden = false;
            let mut ap = false;
            let mut key_mgmt: Option<String> = None;
            let mut pmf: Option<String> = None;
            let mut i = 2;
            while i + 1 < strs.len() {
                match strs[i] {
                    "con-name" => name = strs[i + 1],
                    "ssid" => ssid = strs[i + 1],
                    "802-11-wireless.hidden" => hidden = strs[i + 1] == "yes",
                    "802-11-wireless.mode" => ap = strs[i + 1] == "ap",
                    "wifi-sec.key-mgmt" => key_mgmt = Some(strs[i + 1].to_string()),
                    "wifi-sec.pmf" => pmf = Some(strs[i + 1].to_string()),
                    _ => {}
                }
                i += 2;
            }
            self.calls.push(vec![
                "add".to_string(),
                name.to_string(),
                ssid.to_string(),
                key_mgmt.clone().unwrap_or_default(),
                hidden.to_string(),
                ap.to_string(),
            ]);
            let uuid = self.add_conn(
                name,
                Some(ssid),
                WIFI_TYPE,
                0,
                0,
                None,
                hidden,
                if ap { "ap" } else { "infrastructure" },
                false,
            );
            if let Some(c) = self.get_conn_mut(&uuid) {
                c.key_mgmt = key_mgmt;
                c.pmf = pmf;
            }
            return (
                0,
                format!("Connection '{name}' ({uuid}) successfully added.\n"),
                String::new(),
            );
        }

        // 7. connection edit <uuid>
        if strs.len() == 3 && strs[0] == "connection" && strs[1] == "edit" {
            let uuid = strs[2];
            self.calls.push(vec!["psk".to_string(), uuid.to_string()]);
            let mut psk_val = None;
            if let Some(script) = input_text {
                for line in script.lines() {
                    if let Some(rest) = line.strip_prefix("set 802-11-wireless-security.psk ") {
                        psk_val = Some(rest.to_string());
                    }
                }
            }
            let Some(c) = self.get_conn_mut(uuid) else {
                return (10, String::new(), "Error: connection not found".to_string());
            };
            c.psk = psk_val;
            return (
                0,
                format!("Connection '{}' successfully saved.\n", c.name),
                String::new(),
            );
        }

        // 8. -w <wait> connection up <uuid> ifname <iface>
        if strs.len() >= 7 && strs[0] == "-w" && strs[2] == "connection" && strs[3] == "up" {
            let wait = strs[1];
            let uuid = strs[4];
            self.calls
                .push(vec!["up".to_string(), uuid.to_string(), wait.to_string()]);
            let Some(conn) = self.get_conn(uuid).cloned() else {
                return (10, String::new(), "Error: connection not found".to_string());
            };
            if conn.mode == "ap" {
                if self.flaky_ap_once {
                    self.flaky_ap_once = false;
                    return (
                        4,
                        String::new(),
                        "Error: Connection activation failed: The base network connection was interrupted".to_string(),
                    );
                }
                if !self.ap_ok {
                    return (4, String::new(), "Error: ap failed".to_string());
                }
                self.active = Some(uuid.to_string());
                return (0, String::new(), String::new());
            }
            let ssid = conn.ssid.as_deref().unwrap_or("");
            let Some(air) = self.air.iter().find(|a| a.ssid == ssid).cloned() else {
                self.active = None;
                return (
                    4,
                    String::new(),
                    "Error: Connection activation failed: The Wi-Fi network could not be found"
                        .to_string(),
                );
            };
            if let Some(expected_pw) = air.password.as_deref() {
                if !expected_pw.is_empty() && conn.psk.as_deref() != Some(expected_pw) {
                    self.active = None;
                    return (
                        4,
                        String::new(),
                        "Error: Connection activation failed: Secrets were required, but not provided".to_string(),
                    );
                }
            }
            self.active = Some(uuid.to_string());
            let new_ts = 1_790_000_000 + self.n;
            if let Some(c) = self.get_conn_mut(uuid) {
                c.timestamp = new_ts;
            }
            return (0, String::new(), String::new());
        }

        // 9. connection down <uuid>
        if strs.len() == 3 && strs[0] == "connection" && strs[1] == "down" {
            let uuid = strs[2];
            self.calls.push(vec!["down".to_string(), uuid.to_string()]);
            if self.active.as_deref() == Some(uuid) {
                self.active = None;
            }
            return (0, String::new(), String::new());
        }

        // 10. connection delete <uuid>
        if strs.len() == 3 && strs[0] == "connection" && strs[1] == "delete" {
            let uuid = strs[2];
            self.calls
                .push(vec!["delete".to_string(), uuid.to_string()]);
            if self.active.as_deref() == Some(uuid) {
                self.active = None;
            }
            self.conns.retain(|c| c.uuid != uuid);
            return (0, String::new(), String::new());
        }

        // 11. connection modify <uuid> [k v ...]
        if strs.len() >= 5 && strs[0] == "connection" && strs[1] == "modify" {
            let uuid = strs[2];
            let mut call = vec!["modify".to_string(), uuid.to_string()];
            for s in &strs[3..] {
                call.push((*s).to_string());
            }
            self.calls.push(call);
            if let Some(c) = self.get_conn_mut(uuid) {
                let mut i = 3;
                while i + 1 < strs.len() {
                    match strs[i] {
                        "connection.autoconnect-priority" => {
                            c.priority = strs[i + 1].parse::<i32>().unwrap_or(0);
                        }
                        "connection.autoconnect" => {
                            c.autoconnect = strs[i + 1] == "yes";
                        }
                        "802-11-wireless-security.pmf" => {
                            c.pmf = Some(strs[i + 1].to_string());
                        }
                        _ => {}
                    }
                    i += 2;
                }
            }
            return (0, String::new(), String::new());
        }

        (
            1,
            String::new(),
            format!("Unsupported fake nmcli args: {:?}", args),
        )
    }
}

#[derive(Clone, Default)]
pub struct FakeNm {
    pub state: Arc<Mutex<FakeState>>,
}

impl FakeNm {
    pub fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(FakeState::default())),
        }
    }

    pub fn with_state<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&mut FakeState) -> R,
    {
        let mut st = self.state.lock().unwrap();
        f(&mut st)
    }
}

impl CommandRunner for FakeNm {
    fn run(
        &self,
        args: &[String],
        input_text: Option<&str>,
        _timeout: Duration,
    ) -> (i32, String, String) {
        let mut st = self.state.lock().unwrap();
        st.eval_cli(args, input_text)
    }
}

/// 供外部程序模式（`piwifi --fake-nmcli <state.json> <nmcli-args...>`）讀寫狀態檔並模擬 nmcli。
pub fn run_fake_nmcli_process(state_path: &Path, args: &[String], input_text: Option<&str>) -> i32 {
    let mut state: FakeState = if state_path.exists() {
        let raw = fs::read_to_string(state_path).unwrap_or_default();
        serde_json::from_str(&raw).unwrap_or_default()
    } else {
        FakeState::default()
    };
    let (rc, out, err) = state.eval_cli(args, input_text);
    if let Ok(serialized) = serde_json::to_string_pretty(&state) {
        let _ = fs::write(state_path, serialized);
    }
    if !out.is_empty() {
        print!("{out}");
    }
    if !err.is_empty() {
        eprint!("{err}");
    }
    rc
}
