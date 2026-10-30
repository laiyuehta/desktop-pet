use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex, RwLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::log::fmt_ssid;
use crate::nm::{
    key_mgmt_for, read_hotspot_conf, result_text, NmBackend, NmConnection, NmError,
    DEFAULT_HOTSPOT_CONF, HOTSPOT_PORT, HOTSPOT_SSID, PRIORITY_BASE, PRIORITY_STEP, WIFI_TYPE,
};
use crate::{log_info, log_warn};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ManagerError {
    Observe,
    PinRequired,
    Validation(String),
    Nm(NmError),
}

impl ManagerError {
    pub fn kind(&self) -> &str {
        match self {
            ManagerError::Observe => "observe_mode",
            ManagerError::PinRequired => "pin_required",
            ManagerError::Validation(_) => "bad_request",
            ManagerError::Nm(e) => &e.kind,
        }
    }

    pub fn message(&self) -> String {
        match self {
            ManagerError::Observe => "observe 模式".to_string(),
            ManagerError::PinRequired => "需要 PIN".to_string(),
            ManagerError::Validation(msg) => msg.clone(),
            ManagerError::Nm(e) => result_text(&e.kind).to_string(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct SavedNetwork {
    pub uuid: String,
    pub name: String,
    pub ssid: String,
    pub hidden: bool,
    pub secured: bool,
    pub priority: i32,
    pub active: bool,
    pub last_connected: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ScanNetwork {
    pub ssid: String,
    pub signal: i32,
    pub security: String,
    pub secured: bool,
    pub active: bool,
    pub saved: bool,
    pub uuid: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct WiredStatus {
    pub device: String,
    pub state: String,
    pub connection: String,
    pub ip: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct WifiInfoStatus {
    pub device: String,
    pub state: String,
    pub ssid: Option<String>,
    pub signal: Option<i32>,
    pub ip: Option<String>,
    pub uuid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hotspot: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct HotspotStatus {
    pub ssid: String,
    pub active: bool,
    pub url: Option<String>,
    pub enabled: bool,
    pub configured: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct OpStatus {
    pub id: String,
    pub ssid: Option<String>,
    pub uuid: Option<String>,
    pub phase: String,
    pub result: Option<String>,
    pub message: String,
    pub reverted: bool,
    pub started: f64,
    pub finished: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct DaemonStatus {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ok: Option<bool>,
    pub name: String,
    pub version: String,
    pub proto: u32,
    pub pid: u32,
    pub started: f64,
    pub uptime: f64,
    pub clients: usize,
    pub errors: u64,
    pub last_error: Option<String>,
    pub last_error_ts: Option<f64>,
    pub observe: bool,
    pub mode: String,
    pub wifi: WifiInfoStatus,
    pub wired: Vec<WiredStatus>,
    pub pin_required: bool,
    pub hotspot: HotspotStatus,
    pub op: Option<OpStatus>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct HotspotEventInfo {
    pub ssid: String,
    pub active: bool,
    pub url: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct StateEvent {
    pub event: String,
    pub v: u32,
    pub ts: f64,
    pub observe: bool,
    pub mode: String,
    pub ssid: Option<String>,
    pub ip: Option<String>,
    pub hotspot: HotspotEventInfo,
    pub notice: Option<String>,
    pub op: Option<OpStatus>,
}

pub fn now_epoch_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 將 Unix epoch 秒數轉為 ISO-8601 時間字串（UTC `+0000`）。
pub fn format_timestamp_iso(ts: u64) -> String {
    let days = (ts / 86_400) as i64;
    let rem = ts % 86_400;
    let hour = rem / 3600;
    let min = (rem % 3600) / 60;
    let sec = rem % 60;

    // Howard Hinnant's civil_from_days
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = (yoe as i64) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };

    format!("{year:04}-{m:02}-{d:02}T{hour:02}:{min:02}:{sec:02}+0000")
}

#[derive(Clone)]
pub struct WifiConfig {
    pub iface: String,
    pub try_secs: u32,
    pub rescan_secs: f64,
    pub revert_secs: u32,
    pub hotspot_after: f64,
    pub hotspot_retry: f64,
    pub hotspot_enabled: bool,
    pub hotspot_conf: PathBuf,
    pub hotspot_ssid: String,
    pub hotspot_url_fallback: String,
    pub observe: bool,
    pub redact_ssid: bool,
    pub pin: Option<String>,
}

impl Default for WifiConfig {
    fn default() -> Self {
        Self {
            iface: "wlan0".to_string(),
            try_secs: 20,
            rescan_secs: 60.0,
            revert_secs: 30,
            hotspot_after: 120.0,
            hotspot_retry: 180.0,
            hotspot_enabled: true,
            hotspot_conf: PathBuf::from(DEFAULT_HOTSPOT_CONF),
            hotspot_ssid: HOTSPOT_SSID.to_string(),
            hotspot_url_fallback: std::env::var("PIWIFI_HOTSPOT_URL")
                .or_else(|_| std::env::var("PET_HOTSPOT_URL"))
                .unwrap_or_default(),
            observe: false,
            redact_ssid: false,
            pin: None,
        }
    }
}

pub type ClockFn = Arc<dyn Fn() -> f64 + Send + Sync>;
pub type SleepFn = Arc<dyn Fn(Duration) + Send + Sync>;
pub type NotifyFn = Arc<dyn Fn(&str, f64) + Send + Sync>;

struct InnerState {
    mode: String,
    op: Option<OpStatus>,
    disconnected_since: Option<f64>,
    next_round: f64,
    hotspot_since: Option<f64>,
    last_web: f64,
    last_notice: String,
    last_notice_at: f64,
    hotspot_checked: bool,
    status_cache: Option<(f64, DaemonStatus)>,
    clients: usize,
    errors: u64,
    last_error: Option<String>,
    last_error_ts: Option<f64>,
    last_pushed_sig: String,
}

pub struct WifiManager {
    pub nm: Arc<dyn NmBackend>,
    pub cfg: WifiConfig,
    pub clock: ClockFn,
    pub sleep_fn: SleepFn,
    pub notify_fn: NotifyFn,
    pub started_epoch: f64,
    pub started_mono: f64,
    op_lock: Mutex<()>,
    state: RwLock<InnerState>,
    subscribers: Mutex<Vec<Sender<StateEvent>>>,
    op_counter: AtomicU64,
}

impl WifiManager {
    pub fn new(nm: Arc<dyn NmBackend>, cfg: WifiConfig) -> Arc<Self> {
        let start_inst = Instant::now();
        let clock: ClockFn = Arc::new(move || start_inst.elapsed().as_secs_f64());
        let sleep_fn: SleepFn = Arc::new(thread::sleep);
        let notify_fn: NotifyFn = Arc::new(|_, _| {});
        Self::with_hooks(nm, cfg, clock, sleep_fn, notify_fn)
    }

    pub fn with_hooks(
        nm: Arc<dyn NmBackend>,
        cfg: WifiConfig,
        clock: ClockFn,
        sleep_fn: SleepFn,
        notify_fn: NotifyFn,
    ) -> Arc<Self> {
        let started_mono = clock();
        Arc::new(Self {
            nm,
            cfg,
            clock,
            sleep_fn,
            notify_fn,
            started_epoch: now_epoch_secs(),
            started_mono,
            op_lock: Mutex::new(()),
            state: RwLock::new(InnerState {
                mode: "unknown".to_string(),
                op: None,
                disconnected_since: None,
                next_round: 0.0,
                hotspot_since: None,
                last_web: -1e9,
                last_notice: String::new(),
                last_notice_at: -1e9,
                hotspot_checked: false,
                status_cache: None,
                clients: 0,
                errors: 0,
                last_error: None,
                last_error_ts: None,
                last_pushed_sig: String::new(),
            }),
            subscribers: Mutex::new(Vec::new()),
            op_counter: AtomicU64::new(1),
        })
    }

    pub fn now(&self) -> f64 {
        (self.clock)()
    }

    fn log_ssid(&self, ssid: &str) -> String {
        fmt_ssid(ssid, self.cfg.redact_ssid)
    }

    pub fn record_error(&self, msg: impl Into<String>) {
        let m = msg.into();
        if let Ok(mut st) = self.state.write() {
            st.errors += 1;
            st.last_error = Some(m);
            st.last_error_ts = Some(now_epoch_secs());
        }
    }

    pub fn client_connected(&self) {
        if let Ok(mut st) = self.state.write() {
            st.clients += 1;
        }
    }

    pub fn client_disconnected(&self) {
        if let Ok(mut st) = self.state.write() {
            st.clients = st.clients.saturating_sub(1);
        }
    }

    pub fn authorize(&self, pin: Option<&str>) -> Result<(), ManagerError> {
        if let Some(expected) = self.cfg.pin.as_deref() {
            if !expected.is_empty() && pin != Some(expected) {
                return Err(ManagerError::PinRequired);
            }
        }
        Ok(())
    }

    pub fn subscribe(&self) -> std::sync::mpsc::Receiver<StateEvent> {
        let (tx, rx) = std::sync::mpsc::channel();
        let initial = self.current_event();
        let _ = tx.send(initial);
        if let Ok(mut subs) = self.subscribers.lock() {
            subs.push(tx);
        }
        rx
    }

    pub fn current_event(&self) -> StateEvent {
        let st = self.status(0.0);
        let notice = self
            .state
            .read()
            .ok()
            .map(|s| s.last_notice.clone())
            .filter(|n| !n.is_empty());
        StateEvent {
            event: "state".to_string(),
            v: 1,
            ts: now_epoch_secs(),
            observe: self.cfg.observe,
            mode: st.mode,
            ssid: st.wifi.ssid,
            ip: st.wifi.ip,
            hotspot: HotspotEventInfo {
                ssid: st.hotspot.ssid,
                active: st.hotspot.active,
                url: st.hotspot.url,
            },
            notice,
            op: st.op,
        }
    }

    fn maybe_broadcast(&self) {
        let ev = self.current_event();
        let sig = format!(
            "{}|{:?}|{:?}|{}|{:?}|{:?}|{:?}",
            ev.mode,
            ev.ssid,
            ev.ip,
            ev.hotspot.active,
            ev.hotspot.url,
            ev.notice,
            ev.op
                .as_ref()
                .map(|o| (&o.id, &o.phase, &o.result, &o.message))
        );
        {
            let mut st = self.state.write().unwrap();
            if st.last_pushed_sig == sig {
                return;
            }
            st.last_pushed_sig = sig;
        }
        if let Ok(mut subs) = self.subscribers.lock() {
            subs.retain(|tx| tx.send(ev.clone()).is_ok());
        }
    }

    fn invalidate_cache(&self) {
        if let Ok(mut st) = self.state.write() {
            st.status_cache = None;
        }
    }

    fn say(&self, text: &str, secs: f64, repeat_every: f64) {
        let now = self.now();
        {
            let mut st = self.state.write().unwrap();
            if st.last_notice == text
                && (repeat_every <= 0.0 || now - st.last_notice_at < repeat_every)
            {
                return;
            }
            st.last_notice = text.to_string();
            st.last_notice_at = now;
        }
        (self.notify_fn)(text, secs);
    }

    fn active_wifi(&self, conns: Option<&[NmConnection]>) -> Result<Option<NmConnection>, NmError> {
        let owned;
        let list = match conns {
            Some(c) => c,
            None => {
                owned = self.nm.connections()?;
                &owned
            }
        };
        Ok(list
            .iter()
            .find(|c| c.conn_type == WIFI_TYPE && c.active && c.device == self.cfg.iface)
            .cloned())
    }

    fn is_hotspot(&self, conn: &NmConnection) -> Result<bool, NmError> {
        if conn.name == self.cfg.hotspot_ssid {
            return Ok(true);
        }
        let info = self.nm.wifi_info(&conn.uuid)?;
        Ok(info.mode == "ap")
    }

    pub fn saved(&self) -> Result<Vec<SavedNetwork>, NmError> {
        let conns = self.nm.connections()?;
        let mut items: Vec<(SavedNetwork, u64)> = Vec::new();
        for c in conns {
            if c.conn_type != WIFI_TYPE || c.name == self.cfg.hotspot_ssid {
                continue;
            }
            let info = self.nm.wifi_info(&c.uuid)?;
            if info.mode != "infrastructure" {
                continue;
            }
            let last_connected = if c.timestamp > 0 {
                Some(format_timestamp_iso(c.timestamp))
            } else {
                None
            };
            items.push((
                SavedNetwork {
                    uuid: c.uuid,
                    name: c.name,
                    ssid: info.ssid,
                    hidden: info.hidden,
                    secured: info.secured,
                    priority: c.priority,
                    active: c.active,
                    last_connected,
                },
                c.timestamp,
            ));
        }
        items.sort_by(|(a, ts_a), (b, ts_b)| {
            b.priority.cmp(&a.priority).then_with(|| ts_b.cmp(ts_a))
        });
        Ok(items.into_iter().map(|(s, _)| s).collect())
    }

    pub fn scan(&self, rescan: bool) -> Result<Vec<ScanNetwork>, NmError> {
        let do_rescan = if self.cfg.observe { false } else { rescan };
        let nets = match self.nm.wifi_list(&self.cfg.iface, do_rescan) {
            Ok(n) => n,
            Err(_) => self.nm.wifi_list(&self.cfg.iface, false)?,
        };
        let saved_list = self.saved()?;
        let mut saved_by_ssid: HashMap<String, SavedNetwork> = HashMap::new();
        for s in saved_list {
            saved_by_ssid.entry(s.ssid.clone()).or_insert(s);
        }
        let mut best: HashMap<String, crate::nm::NmWifiAp> = HashMap::new();
        for n in nets {
            if n.ssid.is_empty() || n.ssid == self.cfg.hotspot_ssid {
                continue;
            }
            let replace = match best.get(&n.ssid) {
                None => true,
                Some(cur) => n.signal > cur.signal || n.in_use,
            };
            if replace {
                best.insert(n.ssid.clone(), n);
            }
        }
        let mut out: Vec<ScanNetwork> = best
            .into_iter()
            .map(|(ssid, n)| {
                let saved_entry = saved_by_ssid.get(&ssid);
                let secured = !n.security.is_empty() && n.security != "--";
                ScanNetwork {
                    ssid,
                    signal: n.signal,
                    security: n.security,
                    secured,
                    active: n.in_use,
                    saved: saved_entry.is_some(),
                    uuid: saved_entry.map(|s| s.uuid.clone()),
                }
            })
            .collect();
        out.sort_by(|a, b| {
            b.active
                .cmp(&a.active)
                .then_with(|| b.signal.cmp(&a.signal))
                .then_with(|| a.ssid.cmp(&b.ssid))
        });
        Ok(out)
    }

    fn hotspot_ip(&self, mode: &str) -> Option<String> {
        if mode != "hotspot" {
            return None;
        }
        self.nm.device_ip(&self.cfg.iface).ok().flatten()
    }

    pub fn hotspot_url(&self) -> Option<String> {
        let mode = self.state.read().unwrap().mode.clone();
        self.hotspot_url_for_mode(&mode)
    }

    fn hotspot_url_for_mode(&self, mode: &str) -> Option<String> {
        if let Some(ip) = self.hotspot_ip(mode) {
            return Some(format!("http://{ip}:{HOTSPOT_PORT}/"));
        }
        if !self.cfg.hotspot_url_fallback.is_empty() {
            return Some(self.cfg.hotspot_url_fallback.clone());
        }
        None
    }

    fn hotspot_hostport(&self, mode: &str) -> String {
        if let Some(ip) = self.hotspot_ip(mode) {
            format!("{ip}:{HOTSPOT_PORT}")
        } else {
            format!("熱點閘道:{HOTSPOT_PORT}")
        }
    }

    pub fn status(&self, cache_s: f64) -> DaemonStatus {
        let now = self.now();
        {
            let st = self.state.read().unwrap();
            if let Some((cached_at, ref cached)) = st.status_cache {
                if cache_s > 0.0 && now - cached_at < cache_s {
                    let mut copy = cached.clone();
                    copy.op = st.op.clone();
                    copy.clients = st.clients;
                    copy.errors = st.errors;
                    copy.last_error = st.last_error.clone();
                    copy.last_error_ts = st.last_error_ts;
                    copy.uptime = (now - self.started_mono).max(0.0);
                    return copy;
                }
            }
        }

        let mut wifi = WifiInfoStatus {
            device: self.cfg.iface.clone(),
            state: "unavailable".to_string(),
            ssid: None,
            signal: None,
            ip: None,
            uuid: None,
            hotspot: None,
            error: None,
        };
        let mut wired = Vec::new();

        let query_res: Result<(), NmError> = (|| {
            let devs = self.nm.devices()?;
            let conns = self.nm.connections()?;
            for d in devs {
                if d.device == self.cfg.iface {
                    wifi.state = d.state;
                } else if d.dev_type == "ethernet" {
                    let ip = if d.state.starts_with("connected") {
                        self.nm.device_ip(&d.device).ok().flatten()
                    } else {
                        None
                    };
                    wired.push(WiredStatus {
                        device: d.device,
                        state: d.state,
                        connection: d.connection,
                        ip,
                    });
                }
            }
            if let Some(act) = self.active_wifi(Some(&conns))? {
                let info = self.nm.wifi_info(&act.uuid)?;
                let is_ap = info.mode == "ap";
                wifi.ssid = Some(info.ssid);
                wifi.uuid = Some(act.uuid);
                wifi.ip = self.nm.device_ip(&self.cfg.iface).ok().flatten();
                wifi.hotspot = Some(is_ap);
                if !is_ap {
                    if let Ok(aps) = self.nm.wifi_list(&self.cfg.iface, false) {
                        for n in aps {
                            if n.in_use {
                                wifi.signal = Some(n.signal);
                                break;
                            }
                        }
                    }
                }
            }
            Ok(())
        })();

        if let Err(e) = query_res {
            wifi.error = Some(e.msg.clone());
            self.record_error(e.msg);
        }

        let st_guard = self.state.write().unwrap();
        let mode = st_guard.mode.clone();
        drop(st_guard);

        let hs_url = self.hotspot_url_for_mode(&mode);
        let configured = read_hotspot_conf(&self.cfg.hotspot_conf).is_some();

        let mut st_guard = self.state.write().unwrap();
        let status_obj = DaemonStatus {
            ok: Some(true),
            name: "piwifi".to_string(),
            version: env!("CARGO_PKG_VERSION").to_string(),
            proto: 1,
            pid: std::process::id(),
            started: self.started_epoch,
            uptime: (now - self.started_mono).max(0.0),
            clients: st_guard.clients,
            errors: st_guard.errors,
            last_error: st_guard.last_error.clone(),
            last_error_ts: st_guard.last_error_ts,
            observe: self.cfg.observe,
            mode: st_guard.mode.clone(),
            wifi,
            wired,
            pin_required: self
                .cfg
                .pin
                .as_ref()
                .map(|p| !p.is_empty())
                .unwrap_or(false),
            hotspot: HotspotStatus {
                ssid: self.cfg.hotspot_ssid.clone(),
                active: st_guard.mode == "hotspot",
                url: hs_url,
                enabled: self.cfg.hotspot_enabled,
                configured,
            },
            op: st_guard.op.clone(),
        };
        st_guard.status_cache = Some((now, status_obj.clone()));
        status_obj
    }

    pub fn start_connect(
        self: &Arc<Self>,
        ssid: Option<String>,
        password: Option<String>,
        hidden: bool,
        uuid: Option<String>,
        background: bool,
    ) -> Result<OpStatus, ManagerError> {
        let clean_ssid = ssid.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        let clean_uuid = uuid.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        if self.cfg.observe {
            let target_desc = clean_ssid
                .as_deref()
                .map(|s| self.log_ssid(s))
                .or_else(|| clean_uuid.clone())
                .unwrap_or_else(|| "?".to_string());
            log_info!(
                "[observe] 若為正式模式：將執行 connect（目標={}，hidden={}）",
                target_desc,
                hidden
            );
            return Err(ManagerError::Observe);
        }
        if clean_uuid.is_none() && clean_ssid.is_none() {
            return Err(ManagerError::Validation("需要 ssid 或 uuid".to_string()));
        }
        if let Some(ref pw) = password {
            if !pw.is_empty() && !(8..=63).contains(&pw.len()) {
                return Err(ManagerError::Validation(
                    "WPA 密碼長度要 8～63 個字".to_string(),
                ));
            }
        }
        let now = self.now();
        let op_id = format!("{:08x}", self.op_counter.fetch_add(1, Ordering::Relaxed));
        let op = OpStatus {
            id: op_id,
            ssid: clean_ssid.clone(),
            uuid: clean_uuid.clone(),
            phase: "connecting".to_string(),
            result: None,
            message: "連線中…".to_string(),
            reverted: false,
            started: now_epoch_secs(),
            finished: None,
        };
        {
            let mut st = self.state.write().unwrap();
            if let Some(ref cur_op) = st.op {
                if cur_op.phase != "done" {
                    return Err(ManagerError::Validation("正在連線中，請稍候".to_string()));
                }
            }
            st.last_web = now;
            st.op = Some(op.clone());
        }

        let pw_opt = password.filter(|p| !p.is_empty());
        if background {
            let mgr = Arc::clone(self);
            thread::Builder::new()
                .name("wifi-connect".to_string())
                .spawn(move || {
                    mgr.connect_job(clean_ssid, pw_opt, hidden, clean_uuid);
                })
                .ok();
            Ok(op)
        } else {
            self.connect_job(clean_ssid, pw_opt, hidden, clean_uuid);
            let final_op = self.state.read().unwrap().op.clone().unwrap_or(op);
            Ok(final_op)
        }
    }

    fn connect_job(
        &self,
        ssid: Option<String>,
        password: Option<String>,
        hidden: bool,
        uuid: Option<String>,
    ) {
        let _guard = self.op_lock.lock().unwrap();
        if let Err(e) = self.do_connect(ssid, password, hidden, uuid) {
            let msg = match e {
                ManagerError::Validation(m) => m,
                ManagerError::Nm(nm_err) => nm_err.msg,
                other => other.message(),
            };
            self.record_error(format!("Wi-Fi 連線工作失敗：{msg}"));
            let mut st = self.state.write().unwrap();
            if let Some(ref mut op) = st.op {
                op.phase = "done".to_string();
                op.result = Some("failed".to_string());
                op.message = format!("連線失敗：{msg}");
                op.finished = Some(now_epoch_secs());
            }
        }
        self.invalidate_cache();
        self.maybe_broadcast();
    }

    fn do_connect(
        &self,
        mut ssid: Option<String>,
        password: Option<String>,
        hidden: bool,
        uuid: Option<String>,
    ) -> Result<(), ManagerError> {
        let conns = self.nm.connections().map_err(ManagerError::Nm)?;
        let prev = self.active_wifi(Some(&conns)).map_err(ManagerError::Nm)?;
        let prev_uuid = match prev {
            Some(ref p) if !self.is_hotspot(p).unwrap_or(false) => Some(p.uuid.clone()),
            _ => None,
        };
        let saved = self.saved().map_err(ManagerError::Nm)?;
        let mut created: Option<String> = None;
        let mut target: Option<SavedNetwork> = None;

        if let Some(ref u) = uuid {
            let found = saved.iter().find(|s| &s.uuid == u).cloned();
            let Some(t) = found else {
                return Err(ManagerError::Validation(
                    "找不到這個已記錄的網路".to_string(),
                ));
            };
            ssid = Some(t.ssid.clone());
            if let Ok(mut st) = self.state.write() {
                if let Some(ref mut op) = st.op {
                    op.ssid = ssid.clone();
                }
            }
            target = Some(t);
        } else if let Some(ref s_name) = ssid {
            if password.is_none() {
                target = saved.iter().find(|s| &s.ssid == s_name).cloned();
            }
        }

        let ssid_str = ssid.clone().unwrap_or_default();
        self.say(&format!("正在連 Wi‑Fi：{ssid_str}"), 8.0, 0.0);
        {
            let mut st = self.state.write().unwrap();
            st.mode = "connecting".to_string();
        }
        self.maybe_broadcast();

        let connect_attempt: Result<String, NmError> = (|| {
            let up_uuid = if let Some(ref t) = target {
                t.uuid.clone()
            } else {
                let mut sec = String::new();
                if !hidden {
                    for n in self.nm.wifi_list(&self.cfg.iface, false)? {
                        if n.ssid == ssid_str {
                            sec = n.security;
                            break;
                        }
                    }
                }
                let mut km = key_mgmt_for(&sec, password.is_some());
                if km.is_none() && password.is_some() {
                    km = Some("wpa-psk");
                }
                if km.is_some() && password.is_none() {
                    return Err(NmError::new("這個網路需要密碼", "wrong_password"));
                }
                let names: HashSet<String> = conns.iter().map(|c| c.name.clone()).collect();
                let mut name = ssid_str.clone();
                let mut idx = 2;
                while names.contains(&name) {
                    name = format!("{ssid_str} ({idx})");
                    idx += 1;
                }
                let new_u =
                    self.nm
                        .add_wifi(&name, &ssid_str, &self.cfg.iface, km, hidden, false)?;
                created = Some(new_u.clone());
                if let Some(ref pw) = password {
                    self.nm.set_psk(&new_u, pw)?;
                }
                new_u
            };
            let hs_active = self.state.read().unwrap().hotspot_since.is_some();
            if hs_active {
                self.hotspot_off(false);
            }
            self.nm
                .up(&up_uuid, &self.cfg.iface, self.cfg.revert_secs)?;
            Ok(up_uuid)
        })();

        match connect_attempt {
            Err(exc) => {
                let kind = match exc.kind.as_str() {
                    "ok" | "wrong_password" | "not_found" | "timeout" | "not_authorized"
                    | "failed" | "busy" => exc.kind.clone(),
                    _ => "failed".to_string(),
                };
                let r_text = result_text(&kind);
                log_warn!(
                    "連 Wi‑Fi {} 失敗：{}（{}）",
                    self.log_ssid(&ssid_str),
                    r_text,
                    kind
                );
                self.record_error(format!("連 Wi‑Fi 失敗：{r_text}（{kind}）"));
                if let Some(ref c_uuid) = created {
                    let _ = self.nm.delete(c_uuid);
                }
                {
                    let mut st = self.state.write().unwrap();
                    if let Some(ref mut op) = st.op {
                        op.phase = if prev_uuid.is_some() {
                            "reverting".to_string()
                        } else {
                            "done".to_string()
                        };
                        op.result = Some(kind.clone());
                        op.message = if prev_uuid.is_some() {
                            format!("{r_text}，切回原本的網路…")
                        } else {
                            r_text.to_string()
                        };
                    }
                }
                let mut reverted = false;
                let mut final_msg = if prev_uuid.is_some() {
                    format!("{r_text}，切回原本的網路…")
                } else {
                    r_text.to_string()
                };
                if let Some(ref p_uuid) = prev_uuid {
                    match self.nm.up(p_uuid, &self.cfg.iface, self.cfg.revert_secs) {
                        Ok(()) => {
                            reverted = true;
                            final_msg = format!("{r_text}，已切回原本的網路");
                        }
                        Err(exc2) => {
                            final_msg = format!("{r_text}，切回原本的網路也失敗（{}）", exc2.kind);
                        }
                    }
                }
                self.say(r_text, 6.0, 0.0);
                let mut st = self.state.write().unwrap();
                if let Some(ref mut op) = st.op {
                    op.reverted = reverted;
                    op.message = final_msg;
                    op.phase = "done".to_string();
                    op.finished = Some(now_epoch_secs());
                }
                st.mode = if reverted {
                    "connected".to_string()
                } else {
                    "unknown".to_string()
                };
                Ok(())
            }
            Ok(up_uuid) => {
                let new_uuid = created.clone().unwrap_or(up_uuid);
                let _ = self.promote(&new_uuid, created.is_some());
                if created.is_some() {
                    for s in saved {
                        if s.ssid == ssid_str && s.uuid != new_uuid {
                            let _ = self.nm.delete(&s.uuid);
                        }
                    }
                }
                {
                    let mut st = self.state.write().unwrap();
                    if let Some(ref mut op) = st.op {
                        op.phase = "done".to_string();
                        op.result = Some("ok".to_string());
                        op.uuid = Some(new_uuid);
                        op.message = format!("已連上 {ssid_str}");
                        op.finished = Some(now_epoch_secs());
                    }
                    st.mode = "connected".to_string();
                    st.disconnected_since = None;
                }
                self.say(&format!("連上 Wi‑Fi：{ssid_str}"), 5.0, 0.0);
                log_info!("已連上 Wi‑Fi {}（從網頁）", self.log_ssid(&ssid_str));
                Ok(())
            }
        }
    }

    fn promote(&self, uuid: &str, autoconnect: bool) -> Result<(), NmError> {
        let conns = self.nm.connections()?;
        let mut cur = 0;
        let mut max_other: Option<i32> = None;
        for c in &conns {
            if c.uuid == uuid {
                cur = c.priority;
            } else if c.conn_type == WIFI_TYPE && c.name != self.cfg.hotspot_ssid {
                max_other = Some(max_other.map_or(c.priority, |m| m.max(c.priority)));
            }
        }
        let top = max_other.unwrap_or(PRIORITY_BASE - PRIORITY_STEP);
        let mut kv_owned: Vec<String> = Vec::new();
        if cur <= top {
            kv_owned.push("connection.autoconnect-priority".to_string());
            kv_owned.push((top + PRIORITY_STEP).max(PRIORITY_BASE).to_string());
        }
        if autoconnect {
            kv_owned.push("connection.autoconnect".to_string());
            kv_owned.push("yes".to_string());
        }
        if !kv_owned.is_empty() {
            let refs: Vec<&str> = kv_owned.iter().map(String::as_str).collect();
            self.nm.modify(uuid, &refs)?;
        }
        Ok(())
    }

    pub fn forget(&self, uuid: &str) -> Result<String, ManagerError> {
        if self.cfg.observe {
            log_info!("[observe] 若為正式模式：將執行 forget（uuid={}）", uuid);
            return Err(ManagerError::Observe);
        }
        {
            let mut st = self.state.write().unwrap();
            st.last_web = self.now();
        }
        let _guard = self.op_lock.lock().unwrap();
        let saved = self.saved().map_err(ManagerError::Nm)?;
        let Some(target) = saved.into_iter().find(|s| s.uuid == uuid) else {
            return Err(ManagerError::Validation(
                "找不到這個已記錄的網路".to_string(),
            ));
        };
        if target.active {
            return Err(ManagerError::Validation(
                "這是目前連線中的網路，請先連到別的網路再刪除".to_string(),
            ));
        }
        self.nm.delete(uuid).map_err(ManagerError::Nm)?;
        self.invalidate_cache();
        log_info!("已刪除記錄的 Wi‑Fi {}", self.log_ssid(&target.ssid));
        Ok(target.ssid)
    }

    pub fn set_priority(
        &self,
        order: Option<Vec<String>>,
        uuid: Option<String>,
        direction: Option<String>,
        value: Option<i32>,
    ) -> Result<Vec<SavedNetwork>, ManagerError> {
        if self.cfg.observe {
            log_info!(
                "[observe] 若為正式模式：將執行 priority（uuid={:?}, direction={:?}, value={:?}, order={:?}）",
                uuid,
                direction,
                value,
                order
            );
            return Err(ManagerError::Observe);
        }
        {
            let mut st = self.state.write().unwrap();
            st.last_web = self.now();
        }
        let _guard = self.op_lock.lock().unwrap();
        let saved = self.saved().map_err(ManagerError::Nm)?;

        // 支援直接設定單一連線的優先值：priority {uuid, value}
        if let Some(val) = value {
            let u = uuid.ok_or_else(|| {
                ManagerError::Validation("設定 priority value 需要指定 uuid".to_string())
            })?;
            let Some(cur_net) = saved.iter().find(|s| s.uuid == u) else {
                return Err(ManagerError::Validation(
                    "找不到這個已記錄的網路".to_string(),
                ));
            };
            if cur_net.priority != val {
                let val_str = val.to_string();
                self.nm
                    .modify(&u, &["connection.autoconnect-priority", &val_str])
                    .map_err(ManagerError::Nm)?;
            }
            self.invalidate_cache();
            return self.saved().map_err(ManagerError::Nm);
        }

        let mut ids: Vec<String> = saved.iter().map(|s| s.uuid.clone()).collect();
        if let Some(ord) = order {
            let mut a = ord.clone();
            let mut b = ids.clone();
            a.sort();
            b.sort();
            if a != b {
                return Err(ManagerError::Validation(
                    "order 要包含全部已記錄的網路（uuid）".to_string(),
                ));
            }
            ids = ord;
        } else {
            let u = uuid.unwrap_or_default();
            let Some(i) = ids.iter().position(|id| id == &u) else {
                return Err(ManagerError::Validation(
                    "找不到這個已記錄的網路".to_string(),
                ));
            };
            ids.remove(i);
            let j = match direction.as_deref().unwrap_or("") {
                "up" => i.saturating_sub(1),
                "down" => (i + 1).min(ids.len()),
                "top" => 0,
                _ => {
                    return Err(ManagerError::Validation(
                        "direction 要是 up / down / top".to_string(),
                    ))
                }
            };
            ids.insert(j, u);
        }

        let n = ids.len() as i32;
        let cur_map: HashMap<String, i32> =
            saved.into_iter().map(|s| (s.uuid, s.priority)).collect();
        for (i, u) in ids.iter().enumerate() {
            let p = PRIORITY_BASE + PRIORITY_STEP * (n - 1 - (i as i32));
            if cur_map.get(u).copied() != Some(p) {
                let p_str = p.to_string();
                self.nm
                    .modify(u, &["connection.autoconnect-priority", &p_str])
                    .map_err(ManagerError::Nm)?;
            }
        }
        self.invalidate_cache();
        self.saved().map_err(ManagerError::Nm)
    }

    pub fn set_hotspot(&self, on: bool) -> Result<(bool, String), ManagerError> {
        if self.cfg.observe {
            log_info!("[observe] 若為正式模式：將執行 hotspot（on={}）", on);
            return Err(ManagerError::Observe);
        }
        {
            let mut st = self.state.write().unwrap();
            st.last_web = self.now();
        }
        let _guard = self.op_lock.lock().unwrap();
        let ok = if on {
            self.hotspot_on()
        } else {
            self.hotspot_off(true);
            true
        };
        self.invalidate_cache();
        self.maybe_broadcast();
        let mode = self.state.read().unwrap().mode.clone();
        Ok((ok, mode))
    }

    fn hotspot_uuid(&self) -> Option<String> {
        let Some(conf) = read_hotspot_conf(&self.cfg.hotspot_conf) else {
            log_warn!(
                "沒有熱點設定檔 {}（或沒有 password），不開熱點",
                self.cfg.hotspot_conf.display()
            );
            return None;
        };
        let conns = self.nm.connections().ok()?;
        for c in conns {
            if c.name == self.cfg.hotspot_ssid && c.conn_type == WIFI_TYPE {
                let checked = self.state.read().unwrap().hotspot_checked;
                if !checked {
                    let _ = self
                        .nm
                        .modify(&c.uuid, &["802-11-wireless-security.pmf", "disable"]);
                    self.state.write().unwrap().hotspot_checked = true;
                }
                return Some(c.uuid);
            }
        }
        let ssid = conf.ssid.as_deref().unwrap_or(&self.cfg.hotspot_ssid);
        let u = self
            .nm
            .add_wifi(
                &self.cfg.hotspot_ssid,
                ssid,
                &self.cfg.iface,
                Some("wpa-psk"),
                false,
                true,
            )
            .ok()?;
        if self.nm.set_psk(&u, &conf.password).is_err() {
            let _ = self.nm.delete(&u);
            return None;
        }
        log_info!("已建立設定熱點連線 {}", self.cfg.hotspot_ssid);
        Some(u)
    }

    fn hotspot_on(&self) -> bool {
        let Some(u) = self.hotspot_uuid() else {
            return false;
        };
        for attempt in [1, 2] {
            match self.nm.up(&u, &self.cfg.iface, 30) {
                Ok(()) => break,
                Err(exc) => {
                    if attempt == 1 && exc.msg.to_lowercase().contains("interrupted") {
                        log_info!("開熱點被打斷，5 秒後再試一次");
                        (self.sleep_fn)(Duration::from_secs(5));
                        continue;
                    }
                    log_warn!("開熱點失敗：{}", exc);
                    self.record_error(format!("開熱點失敗：{}", exc.msg));
                    return false;
                }
            }
        }
        let now = self.now();
        {
            let mut st = self.state.write().unwrap();
            st.mode = "hotspot".to_string();
            st.hotspot_since = Some(now);
        }
        let url = self.hotspot_url_for_mode("hotspot");
        log_info!("設定熱點 {} 已開啟（{:?}）", self.cfg.hotspot_ssid, url);
        let hostport = self.hotspot_hostport("hotspot");
        self.say(
            &format!("設定熱點：{}，連上後開 {}", self.cfg.hotspot_ssid, hostport),
            10.0,
            0.0,
        );
        true
    }

    fn hotspot_off(&self, reconnect: bool) {
        if let Ok(conns) = self.nm.connections() {
            for c in conns {
                if c.name == self.cfg.hotspot_ssid && c.active {
                    let _ = self.nm.down(&c.uuid);
                    log_info!("設定熱點已關閉");
                }
            }
        }
        let mut st = self.state.write().unwrap();
        st.hotspot_since = None;
        st.mode = "disconnected".to_string();
        if reconnect {
            st.next_round = 0.0;
        }
    }

    fn candidates(&self) -> Vec<SavedNetwork> {
        let Ok(saved) = self.saved() else {
            return Vec::new();
        };
        let saved: Vec<SavedNetwork> = saved.into_iter().filter(|s| !s.ssid.is_empty()).collect();
        if saved.is_empty() {
            return Vec::new();
        }
        let visible: HashSet<String> = self
            .nm
            .wifi_list(&self.cfg.iface, true)
            .map(|list| list.into_iter().map(|n| n.ssid).collect())
            .unwrap_or_default();
        saved
            .into_iter()
            .filter(|s| visible.contains(&s.ssid) || s.hidden)
            .collect()
    }

    fn try_saved(&self) -> bool {
        for s in self.candidates() {
            {
                let mut st = self.state.write().unwrap();
                st.mode = "connecting".to_string();
            }
            self.say(&format!("正在連 Wi‑Fi：{}", s.ssid), 6.0, 0.0);
            match self.nm.up(&s.uuid, &self.cfg.iface, self.cfg.try_secs) {
                Ok(()) => {
                    {
                        let mut st = self.state.write().unwrap();
                        st.mode = "connected".to_string();
                    }
                    log_info!("自動連上 Wi‑Fi {}", self.log_ssid(&s.ssid));
                    self.say(&format!("連上 Wi‑Fi：{}", s.ssid), 5.0, 0.0);
                    return true;
                }
                Err(exc) => {
                    log_info!("自動連 {} 失敗：{}", self.log_ssid(&s.ssid), exc.kind);
                }
            }
        }
        false
    }

    pub fn tick(&self) -> String {
        let Ok(_guard) = self.op_lock.try_lock() else {
            return self.state.read().unwrap().mode.clone();
        };
        let res = self.tick_locked();
        self.invalidate_cache();
        self.maybe_broadcast();
        res
    }

    fn tick_locked(&self) -> String {
        let now = self.now();
        let devs = match self.nm.devices() {
            Ok(d) => d,
            Err(exc) => {
                log_warn!("Wi‑Fi 檢查失敗：{}", exc);
                self.record_error(format!("Wi‑Fi 檢查失敗：{}", exc.msg));
                return self.state.read().unwrap().mode.clone();
            }
        };
        let dev = devs.into_iter().find(|d| d.device == self.cfg.iface);
        let Some(dev) = dev else {
            self.state.write().unwrap().mode = "unavailable".to_string();
            return "unavailable".to_string();
        };
        if dev.state == "unavailable" || dev.state == "unmanaged" {
            self.state.write().unwrap().mode = "unavailable".to_string();
            return "unavailable".to_string();
        }

        let act = match self.active_wifi(None) {
            Ok(a) => a,
            Err(exc) => {
                log_warn!("Wi‑Fi 檢查失敗：{}", exc);
                self.record_error(format!("Wi‑Fi 檢查失敗：{}", exc.msg));
                return self.state.read().unwrap().mode.clone();
            }
        };

        if let Some(ref act_conn) = act {
            if self.is_hotspot(act_conn).unwrap_or(false) {
                let (hs_since, last_web) = {
                    let mut st = self.state.write().unwrap();
                    st.mode = "hotspot".to_string();
                    let since = *st.hotspot_since.get_or_insert(now);
                    (since, st.last_web)
                };
                let hostport = self.hotspot_hostport("hotspot");
                self.say(
                    &format!("設定熱點：{}，連上後開 {}", self.cfg.hotspot_ssid, hostport),
                    8.0,
                    30.0,
                );
                let has_saved = self.saved().map(|s| !s.is_empty()).unwrap_or(false);
                if now - hs_since >= self.cfg.hotspot_retry && now - last_web >= 120.0 && has_saved
                {
                    if self.cfg.observe {
                        log_info!(
                            "[observe] 若為正式模式：熱點開了 {:.0} 秒，將先關掉試已記錄的網路",
                            now - hs_since
                        );
                        return "hotspot".to_string();
                    }
                    log_info!("熱點開了 {:.0} 秒，先關掉試已記錄的網路", now - hs_since);
                    self.hotspot_off(false);
                    if self.try_saved() {
                        self.state.write().unwrap().disconnected_since = None;
                        return self.state.read().unwrap().mode.clone();
                    }
                    self.hotspot_on();
                }
                return self.state.read().unwrap().mode.clone();
            }

            let (was_connected, had_disc) = {
                let st = self.state.read().unwrap();
                (st.mode == "connected", st.disconnected_since.is_some())
            };
            if !was_connected && had_disc {
                if let Ok(info) = self.nm.wifi_info(&act_conn.uuid) {
                    self.say(&format!("連上 Wi‑Fi：{}", info.ssid), 5.0, 0.0);
                }
            }
            let mut st = self.state.write().unwrap();
            st.mode = "connected".to_string();
            st.disconnected_since = None;
            return "connected".to_string();
        }

        if dev.state.starts_with("connecting")
            || dev.state.contains("config")
            || dev.state.contains("ip")
            || dev.state.contains("prepare")
            || dev.state.contains("need-auth")
        {
            self.state.write().unwrap().mode = "connecting".to_string();
            return "connecting".to_string();
        }

        // 沒連上
        let (disc_since, next_round) = {
            let mut st = self.state.write().unwrap();
            if st.disconnected_since.is_none() {
                st.disconnected_since = Some(now);
                st.next_round = now;
                if self.cfg.observe {
                    log_info!("[observe] Wi‑Fi 沒有連線（若為正式模式將開始自動嘗試已記錄的網路）");
                } else {
                    log_info!("Wi‑Fi 沒有連線，開始自動嘗試已記錄的網路");
                }
            }
            st.mode = "disconnected".to_string();
            (st.disconnected_since.unwrap(), st.next_round)
        };
        self.say("沒有網路…正在找 Wi‑Fi", 6.0, 60.0);

        if now >= next_round {
            if self.cfg.observe {
                log_info!("[observe] 若為正式模式：將依優先順序自動嘗試已記錄的網路");
                self.state.write().unwrap().next_round = self.now() + self.cfg.rescan_secs;
            } else {
                if self.try_saved() {
                    self.state.write().unwrap().disconnected_since = None;
                    return self.state.read().unwrap().mode.clone();
                }
                let mut st = self.state.write().unwrap();
                st.mode = "disconnected".to_string();
                st.next_round = self.now() + self.cfg.rescan_secs;
            }
        }

        let now_after = self.now();
        if self.cfg.hotspot_enabled && now_after - disc_since >= self.cfg.hotspot_after {
            if self.cfg.observe {
                log_info!(
                    "[observe] 若為正式模式：{:.0} 秒都連不上已記錄的網路，將開設定熱點 {}",
                    now_after - disc_since,
                    self.cfg.hotspot_ssid
                );
            } else {
                log_info!(
                    "{:.0} 秒都連不上已記錄的網路，開設定熱點",
                    now_after - disc_since
                );
                if !self.hotspot_on() {
                    self.state.write().unwrap().next_round = self.now() + self.cfg.rescan_secs;
                }
            }
        }

        self.state.read().unwrap().mode.clone()
    }
}
