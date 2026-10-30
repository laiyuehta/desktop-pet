use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use piwifi::fake_nmcli::FakeState;
use piwifi::manager::{WifiConfig, WifiManager};
use piwifi::nm::{NmBackend, NmCli, SubprocessRunner, WIFI_TYPE};

static INT_DIR_COUNTER: AtomicU64 = AtomicU64::new(1);

struct TempDir {
    path: PathBuf,
}

impl TempDir {
    fn new(prefix: &str) -> Self {
        let id = INT_DIR_COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "piwifi_int_{}_{}_{}",
            prefix,
            std::process::id(),
            id
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Self { path: dir }
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

#[test]
fn test_real_subprocess_fake_nmcli_binary() {
    let tmp = TempDir::new("bin_nmcli");
    let state_path = tmp.path.join("nm_state.json");

    let mut init_state = FakeState::default();
    let home = init_state.add_conn(
        "HomeAP",
        Some("HomeAP"),
        WIFI_TYPE,
        200,
        1_789_000_000,
        Some("homepass1"),
        false,
        "infrastructure",
        true,
    );
    init_state.set_air("HomeAP", Some("homepass1"), "WPA2", 68, false);
    init_state.set_air("Office", Some("officepw123"), "WPA2", 85, false);
    init_state.active = Some(home);
    fs::write(
        &state_path,
        serde_json::to_string_pretty(&init_state).unwrap(),
    )
    .unwrap();

    let bin_path = env!("CARGO_BIN_EXE_piwifi").to_string();
    let runner = SubprocessRunner::with_extra_args(
        bin_path,
        vec![
            "--fake-nmcli".to_string(),
            state_path.to_string_lossy().to_string(),
        ],
    );
    let nm: Arc<dyn NmBackend> = Arc::new(NmCli::new(runner, "wlan0", false));

    let conf = tmp.path.join("hotspot.conf");
    fs::write(&conf, "SSID=PetRescue-AP\nPASSPHRASE=hotspotpw123\n").unwrap();

    let cfg = WifiConfig {
        hotspot_conf: conf,
        hotspot_url_fallback: "http://192.0.2.1:8080/".to_string(),
        ..WifiConfig::default()
    };

    let mgr = WifiManager::new(nm, cfg);
    assert_eq!(mgr.tick(), "connected");

    let st = mgr.status(0.0);
    assert_eq!(st.mode, "connected");
    assert_eq!(st.wifi.ssid.as_deref(), Some("HomeAP"));

    let nets = mgr.scan(true).unwrap();
    assert_eq!(nets.len(), 2);
    assert_eq!(nets[0].ssid, "HomeAP");
    assert!(nets[0].active);

    // 透過外部假 nmcli 執行連線至新網路（密碼走 stdin）
    let op = mgr
        .start_connect(
            Some("Office".to_string()),
            Some("officepw123".to_string()),
            false,
            None,
            false,
        )
        .unwrap();
    assert_eq!(op.result.as_deref(), Some("ok"));
    assert_eq!(mgr.status(0.0).wifi.ssid.as_deref(), Some("Office"));

    // 確認密碼未出現在任何 CLI 參數中（而是透過 stdin 傳遞）
    let after: FakeState = serde_json::from_str(&fs::read_to_string(&state_path).unwrap()).unwrap();
    for argv in &after.raw_cli_calls {
        for arg in argv {
            assert!(
                !arg.contains("officepw123"),
                "密碼不應出現在 nmcli 參數：{argv:?}"
            );
        }
    }
}
