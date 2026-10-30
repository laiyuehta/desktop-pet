use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use crate::log_debug;

pub const DEFAULT_HOTSPOT_CONF: &str = "/etc/piwifi/hotspot.conf";
pub const HOTSPOT_SSID: &str = "PetRescue-AP";
pub const HOTSPOT_PORT: u16 = 8080;
pub const WIFI_TYPE: &str = "802-11-wireless";
pub const PRIORITY_BASE: i32 = 200;
pub const PRIORITY_STEP: i32 = 10;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NmError {
    pub msg: String,
    pub kind: String,
}

impl NmError {
    pub fn new(msg: impl Into<String>, kind: impl Into<String>) -> Self {
        Self {
            msg: msg.into(),
            kind: kind.into(),
        }
    }
}

impl fmt::Display for NmError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.msg)
    }
}

impl std::error::Error for NmError {}

/// 拆 `nmcli -t` 的一行：欄位用 `:` 分隔，值裡的 `:` 和 `\` 會被跳脫成 `\:` 和 `\\`。
pub fn split_terse(line: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut esc = false;
    for ch in line.chars() {
        if esc {
            cur.push(ch);
            esc = false;
        } else if ch == '\\' {
            esc = true;
        } else if ch == ':' {
            out.push(cur);
            cur = String::new();
        } else {
            cur.push(ch);
        }
    }
    out.push(cur);
    out
}

/// 將字串跳脫成 `nmcli -t` 欄位格式（測試替身輸出用）。
pub fn escape_terse(val: &str) -> String {
    let mut out = String::with_capacity(val.len());
    for ch in val.chars() {
        if ch == '\\' || ch == ':' {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

pub fn classify_error(text: &str) -> &'static str {
    let t = text.to_lowercase();
    if t.contains("secrets were required")
        || t.contains("no secrets")
        || t.contains("802-1x supplicant")
        || t.contains("invalid key")
        || (t.contains("psk") && t.contains("invalid"))
    {
        return "wrong_password";
    }
    if t.contains("no network with ssid")
        || t.contains("could not be found")
        || (t.contains("not found") && t.contains("ssid"))
    {
        return "not_found";
    }
    if t.contains("timeout") || t.contains("timed out") {
        return "timeout";
    }
    if t.contains("not authorized")
        || t.contains("insufficient privileges")
        || t.contains("permission")
    {
        return "not_authorized";
    }
    "failed"
}

pub fn result_text(kind: &str) -> &'static str {
    match kind {
        "ok" => "連線成功",
        "wrong_password" => "密碼錯誤",
        "not_found" => "找不到這個網路",
        "timeout" => "連線逾時",
        "not_authorized" => "沒有權限（Pi 上的 polkit 規則還沒裝？）",
        "busy" => "正在處理別的連線",
        _ => "連線失敗",
    }
}

pub fn key_mgmt_for(security: &str, has_password: bool) -> Option<&'static str> {
    let s = security.trim().to_uppercase();
    if !has_password && (s.is_empty() || s == "--" || s == "NONE") {
        return None;
    }
    if s.contains("WEP") {
        return None;
    }
    if s.contains("WPA3") && !s.contains("WPA2") && !s.contains("WPA1") {
        return Some("sae");
    }
    Some("wpa-psk")
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HotspotConf {
    pub ssid: Option<String>,
    pub password: String,
}

pub fn read_hotspot_conf(path: &Path) -> Option<HotspotConf> {
    let content = fs::read_to_string(path).ok()?;
    let mut map = HashMap::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = trimmed.split_once('=') {
            let key = k.trim();
            if !key.starts_with('#') {
                map.insert(key.to_string(), v.trim().to_string());
            }
        }
    }
    let pw = map.get("password")?.clone();
    if pw.is_empty() {
        return None;
    }
    Some(HotspotConf {
        ssid: map.get("ssid").cloned().filter(|s| !s.is_empty()),
        password: pw,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NmConnection {
    pub name: String,
    pub uuid: String,
    pub conn_type: String,
    pub priority: i32,
    pub timestamp: u64,
    pub active: bool,
    pub device: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NmWifiInfo {
    pub ssid: String,
    pub mode: String,
    pub hidden: bool,
    pub secured: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NmDevice {
    pub device: String,
    pub dev_type: String,
    pub state: String,
    pub connection: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NmWifiAp {
    pub in_use: bool,
    pub ssid: String,
    pub signal: i32,
    pub security: String,
    pub chan: String,
}

pub trait CommandRunner: Send + Sync {
    fn run(
        &self,
        args: &[String],
        input_text: Option<&str>,
        timeout: Duration,
    ) -> (i32, String, String);
}

#[derive(Clone, Debug)]
pub struct SubprocessRunner {
    pub binary: String,
    pub extra_args: Vec<String>,
}

impl SubprocessRunner {
    pub fn new(binary: impl Into<String>) -> Self {
        Self {
            binary: binary.into(),
            extra_args: Vec::new(),
        }
    }

    pub fn with_extra_args(binary: impl Into<String>, extra_args: Vec<String>) -> Self {
        Self {
            binary: binary.into(),
            extra_args,
        }
    }
}

impl CommandRunner for SubprocessRunner {
    fn run(
        &self,
        args: &[String],
        input_text: Option<&str>,
        _timeout: Duration,
    ) -> (i32, String, String) {
        let mut cmd = Command::new(&self.binary);
        cmd.args(&self.extra_args)
            .args(args)
            .env("LC_ALL", "C")
            .env("LANG", "C")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if input_text.is_some() {
            cmd.stdin(Stdio::piped());
        } else {
            cmd.stdin(Stdio::null());
        }
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => return (127, String::new(), e.to_string()),
        };
        if let Some(text) = input_text {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(text.as_bytes());
            }
        }
        match child.wait_with_output() {
            Ok(out) => {
                let rc = out.status.code().unwrap_or(1);
                let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
                let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
                (rc, stdout, stderr)
            }
            Err(e) => (127, String::new(), e.to_string()),
        }
    }
}

impl<F> CommandRunner for F
where
    F: Fn(&[String], Option<&str>, Duration) -> (i32, String, String) + Send + Sync,
{
    fn run(
        &self,
        args: &[String],
        input_text: Option<&str>,
        timeout: Duration,
    ) -> (i32, String, String) {
        self(args, input_text, timeout)
    }
}

pub trait NmBackend: Send + Sync {
    fn connections(&self) -> Result<Vec<NmConnection>, NmError>;
    fn wifi_info(&self, uuid: &str) -> Result<NmWifiInfo, NmError>;
    fn devices(&self) -> Result<Vec<NmDevice>, NmError>;
    fn device_ip(&self, dev: &str) -> Result<Option<String>, NmError>;
    fn wifi_list(&self, iface: &str, rescan: bool) -> Result<Vec<NmWifiAp>, NmError>;
    fn add_wifi(
        &self,
        name: &str,
        ssid: &str,
        iface: &str,
        key_mgmt: Option<&str>,
        hidden: bool,
        ap: bool,
    ) -> Result<String, NmError>;
    fn set_psk(&self, uuid: &str, psk: &str) -> Result<(), NmError>;
    fn up(&self, uuid: &str, iface: &str, wait: u32) -> Result<(), NmError>;
    fn down(&self, uuid: &str) -> Result<(), NmError>;
    fn delete(&self, uuid: &str) -> Result<(), NmError>;
    fn modify(&self, uuid: &str, kv: &[&str]) -> Result<(), NmError>;
}

pub struct NmCli<R: CommandRunner> {
    pub runner: R,
    pub allowed_iface: String,
    pub observe: bool,
}

impl<R: CommandRunner> NmCli<R> {
    pub fn new(runner: R, allowed_iface: impl Into<String>, observe: bool) -> Self {
        Self {
            runner,
            allowed_iface: allowed_iface.into(),
            observe,
        }
    }

    fn check_not_observe(&self, action: &str) -> Result<(), NmError> {
        if self.observe {
            return Err(NmError::new(
                format!("observe 模式禁止執行修改指令：{action}"),
                "observe_mode",
            ));
        }
        Ok(())
    }

    fn check_iface(&self, iface: &str) -> Result<(), NmError> {
        if iface != self.allowed_iface {
            return Err(NmError::new(
                format!("禁止操作 {} 以外的網路介面：{}", self.allowed_iface, iface),
                "failed",
            ));
        }
        Ok(())
    }

    pub fn run_cmd(
        &self,
        args: &[&str],
        input_text: Option<&str>,
        timeout: Duration,
        check: bool,
    ) -> Result<String, NmError> {
        let owned: Vec<String> = args.iter().map(|s| (*s).to_string()).collect();
        let (rc, out, err) = self.runner.run(&owned, input_text, timeout);
        // args 裡絕不含密碼（密碼只走 stdin）
        log_debug!("nmcli {} -> {}", args.join(" "), rc);
        if rc != 0 && check {
            let msg = if !err.trim().is_empty() {
                err.trim().to_string()
            } else if !out.trim().is_empty() {
                out.trim().to_string()
            } else {
                format!("nmcli 結束碼 {rc}")
            };
            let kind = classify_error(&msg);
            return Err(NmError::new(msg, kind));
        }
        Ok(out)
    }

    pub fn rows(
        &self,
        args: &[&str],
        timeout: Duration,
        check: bool,
    ) -> Result<Vec<Vec<String>>, NmError> {
        let mut full = Vec::with_capacity(args.len() + 1);
        full.push("-t");
        full.extend_from_slice(args);
        let out = self.run_cmd(&full, None, timeout, check)?;
        Ok(out
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(split_terse)
            .collect())
    }
}

fn extract_uuid(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    for i in 0..bytes.len() {
        if bytes[i] == b'(' && i + 37 < bytes.len() && bytes[i + 37] == b')' {
            let candidate = &text[i + 1..i + 37];
            let valid = candidate.chars().enumerate().all(|(idx, ch)| {
                if matches!(idx, 8 | 13 | 18 | 23) {
                    ch == '-'
                } else {
                    ch.is_ascii_hexdigit()
                }
            });
            if valid {
                return Some(candidate.to_string());
            }
        }
    }
    None
}

impl<R: CommandRunner> NmBackend for NmCli<R> {
    fn connections(&self) -> Result<Vec<NmConnection>, NmError> {
        let rows = self.rows(
            &[
                "-f",
                "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,TIMESTAMP,ACTIVE,DEVICE",
                "connection",
                "show",
            ],
            Duration::from_secs(60),
            true,
        )?;
        let mut out = Vec::new();
        for r in rows {
            if r.len() < 7 {
                continue;
            }
            let priority = r[3].trim().parse::<i32>().unwrap_or(0);
            let timestamp = r[4].trim().parse::<u64>().unwrap_or(0);
            out.push(NmConnection {
                name: r[0].clone(),
                uuid: r[1].clone(),
                conn_type: r[2].clone(),
                priority,
                timestamp,
                active: r[5] == "yes",
                device: r[6].clone(),
            });
        }
        Ok(out)
    }

    fn wifi_info(&self, uuid: &str) -> Result<NmWifiInfo, NmError> {
        let rows = self.rows(
            &[
                "-f",
                "802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.hidden,802-11-wireless-security.key-mgmt",
                "connection",
                "show",
                uuid,
            ],
            Duration::from_secs(60),
            true,
        )?;
        let mut map = HashMap::new();
        for r in rows {
            if r.len() >= 2 {
                map.insert(r[0].clone(), r[1].clone());
            }
        }
        let ssid = map.get("802-11-wireless.ssid").cloned().unwrap_or_default();
        let mode = map
            .get("802-11-wireless.mode")
            .cloned()
            .filter(|m| !m.is_empty())
            .unwrap_or_else(|| "infrastructure".to_string());
        let hidden = map
            .get("802-11-wireless.hidden")
            .map(|v| v == "yes")
            .unwrap_or(false);
        let secured = map
            .get("802-11-wireless-security.key-mgmt")
            .map(|v| !v.is_empty())
            .unwrap_or(false);
        Ok(NmWifiInfo {
            ssid,
            mode,
            hidden,
            secured,
        })
    }

    fn devices(&self) -> Result<Vec<NmDevice>, NmError> {
        let rows = self.rows(
            &["-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            Duration::from_secs(60),
            true,
        )?;
        let mut out = Vec::new();
        for r in rows {
            if r.len() >= 4 {
                out.push(NmDevice {
                    device: r[0].clone(),
                    dev_type: r[1].clone(),
                    state: r[2].clone(),
                    connection: r[3].clone(),
                });
            }
        }
        Ok(out)
    }

    fn device_ip(&self, dev: &str) -> Result<Option<String>, NmError> {
        let rows = self.rows(
            &["-f", "IP4.ADDRESS", "device", "show", dev],
            Duration::from_secs(30),
            false,
        )?;
        for r in rows {
            if r.len() >= 2 && !r[1].is_empty() {
                let ip = r[1].split('/').next().unwrap_or("").trim();
                if !ip.is_empty() {
                    return Ok(Some(ip.to_string()));
                }
            }
        }
        Ok(None)
    }

    fn wifi_list(&self, iface: &str, rescan: bool) -> Result<Vec<NmWifiAp>, NmError> {
        self.check_iface(iface)?;
        // observe 模式下不觸發主動硬體掃描，只讀取快取
        let do_rescan = if self.observe { false } else { rescan };
        let rows = self.rows(
            &[
                "-f",
                "IN-USE,SSID,SIGNAL,SECURITY,CHAN",
                "device",
                "wifi",
                "list",
                "ifname",
                iface,
                "--rescan",
                if do_rescan { "yes" } else { "no" },
            ],
            Duration::from_secs(30),
            true,
        )?;
        let mut out = Vec::new();
        for r in rows {
            if r.len() < 5 {
                continue;
            }
            let signal = r[2].trim().parse::<i32>().unwrap_or(0);
            out.push(NmWifiAp {
                in_use: r[0].trim() == "*",
                ssid: r[1].clone(),
                signal,
                security: r[3].trim().to_string(),
                chan: r[4].clone(),
            });
        }
        Ok(out)
    }

    fn add_wifi(
        &self,
        name: &str,
        ssid: &str,
        iface: &str,
        key_mgmt: Option<&str>,
        hidden: bool,
        ap: bool,
    ) -> Result<String, NmError> {
        self.check_not_observe("connection add")?;
        self.check_iface(iface)?;
        let mut args = vec![
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            iface,
            "con-name",
            name,
            "ssid",
            ssid,
            "autoconnect",
            "no",
            "save",
            "yes",
        ];
        if hidden {
            args.extend_from_slice(&["802-11-wireless.hidden", "yes"]);
        }
        if ap {
            args.extend_from_slice(&[
                "802-11-wireless.mode",
                "ap",
                "802-11-wireless.band",
                "bg",
                "ipv4.method",
                "shared",
                "ipv6.method",
                "disabled",
            ]);
        }
        if let Some(km) = key_mgmt {
            args.extend_from_slice(&["wifi-sec.key-mgmt", km]);
            if ap {
                // Pi 3B（brcmfmac）實測：不關 PMF 熱點會卡在 supplicant-timeout 開不起來
                args.extend_from_slice(&[
                    "wifi-sec.proto",
                    "rsn",
                    "wifi-sec.pairwise",
                    "ccmp",
                    "wifi-sec.group",
                    "ccmp",
                    "wifi-sec.pmf",
                    "disable",
                ]);
            }
        }
        let out = self.run_cmd(&args, None, Duration::from_secs(60), true)?;
        extract_uuid(&out)
            .ok_or_else(|| NmError::new(format!("新增連線失敗：{}", out.trim()), "failed"))
    }

    fn set_psk(&self, uuid: &str, psk: &str) -> Result<(), NmError> {
        self.check_not_observe("connection edit")?;
        if psk.contains('\n') || psk.contains('\r') {
            return Err(NmError::new("密碼不能有換行", "failed"));
        }
        let script = format!("set 802-11-wireless-security.psk {psk}\nsave persistent\nquit\n");
        let args = vec![
            "connection".to_string(),
            "edit".to_string(),
            uuid.to_string(),
        ];
        let (rc, out, err) = self
            .runner
            .run(&args, Some(&script), Duration::from_secs(20));
        log_debug!("nmcli connection edit {} -> {}", uuid, rc);
        let text = format!("{out}{err}").to_lowercase();
        if rc != 0 || text.contains("error") {
            // 不把 nmcli 原始輸出往外丟，避免夾帶密碼內容
            let kind = if text.contains("psk") || text.contains("passphrase") {
                "wrong_password"
            } else {
                "failed"
            };
            return Err(NmError::new("設定密碼失敗（密碼格式不對？）", kind));
        }
        Ok(())
    }

    fn up(&self, uuid: &str, iface: &str, wait: u32) -> Result<(), NmError> {
        self.check_not_observe("connection up")?;
        self.check_iface(iface)?;
        let wait_str = wait.to_string();
        self.run_cmd(
            &["-w", &wait_str, "connection", "up", uuid, "ifname", iface],
            None,
            Duration::from_secs(u64::from(wait) + 15),
            true,
        )?;
        Ok(())
    }

    fn down(&self, uuid: &str) -> Result<(), NmError> {
        self.check_not_observe("connection down")?;
        let _ = self.run_cmd(
            &["connection", "down", uuid],
            None,
            Duration::from_secs(20),
            false,
        );
        Ok(())
    }

    fn delete(&self, uuid: &str) -> Result<(), NmError> {
        self.check_not_observe("connection delete")?;
        self.run_cmd(
            &["connection", "delete", uuid],
            None,
            Duration::from_secs(20),
            true,
        )?;
        Ok(())
    }

    fn modify(&self, uuid: &str, kv: &[&str]) -> Result<(), NmError> {
        self.check_not_observe("connection modify")?;
        let mut args = Vec::with_capacity(kv.len() + 3);
        args.extend_from_slice(&["connection", "modify", uuid]);
        args.extend_from_slice(kv);
        self.run_cmd(&args, None, Duration::from_secs(20), true)?;
        Ok(())
    }
}
