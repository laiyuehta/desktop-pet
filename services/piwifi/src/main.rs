use std::env;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use serde_json::json;

use piwifi::client::{request, subscribe_events};
use piwifi::fake_nmcli::run_fake_nmcli_process;
use piwifi::log::init_logger;
use piwifi::log_info;
use piwifi::manager::{WifiConfig, WifiManager};
use piwifi::nm::{NmCli, SubprocessRunner, DEFAULT_HOTSPOT_CONF, HOTSPOT_SSID};
use piwifi::server::{ServerHandle, DEFAULT_SOCKET_PATH, DEFAULT_STATUS_PATH};

fn env_bool(key: &str) -> bool {
    env::var(key)
        .map(|v| {
            matches!(
                v.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn print_help() {
    println!(
        "\
piwifi {} — Wi‑Fi 與救援熱點守護程式（NetworkManager / nmcli）

用法：
  piwifi [daemon] [選項]       啟動常駐守護程式
  piwifi status [--socket P]   查詢目前狀態（JSON）
  piwifi scan [--socket P]     掃描附近 Wi‑Fi（JSON）
  piwifi saved [--socket P]    列出已記錄的 Wi‑Fi（JSON）
  piwifi events [--socket P]   訂閱狀態變化事件串流（JSON Lines）

選項（或對應環境變數）：
  --observe                    只讀觀察模式（不下任何會改變網路的 nmcli 指令）[PIWIFI_OBSERVE=1]
  --socket <PATH>              Unix socket 路徑（預設 {}）[PIWIFI_SOCKET]
  --status <PATH>              狀態檔路徑（預設 {}）[PIWIFI_STATUS]
  --group <GROUP>              socket 群組（0660）[PIWIFI_GROUP]
  --iface <IFACE>              管理的 Wi‑Fi 介面（預設 wlan0）[PIWIFI_IFACE / PET_WIFI_IFACE]
  --nmcli <PATH>               nmcli 執行檔路徑（預設 nmcli）[PIWIFI_NMCLI]
  --hotspot-conf <PATH>        熱點密碼設定檔（預設 {}）[PIWIFI_HOTSPOT_CONF]
  --hotspot-ssid <SSID>        救援熱點名稱（預設 {}）[PIWIFI_HOTSPOT_SSID]
  --hotspot-url <URL>          熱點備用網址 [PIWIFI_HOTSPOT_URL / PET_HOTSPOT_URL]
  --interval <SECS>            背景檢查間隔秒數（預設 5）
  --try-secs <SECS>            自動重試單一網路逾時秒數（預設 20）
  --rescan-secs <SECS>         自動重試每輪間隔秒數（預設 60）
  --revert-secs <SECS>         切換網路失敗切回逾時秒數（預設 30）
  --hotspot-after <SECS>       斷線多久後開救援熱點（預設 120）
  --hotspot-retry <SECS>       熱點開多久後重試已記錄網路（預設 180）
  --no-hotspot                 停用自動開救援熱點
  --redact-ssid                日誌遮蔽 SSID 名稱 [PIWIFI_REDACT_SSID=1]
  --pin <PIN>                  預留 PIN 驗證 [PIWIFI_PIN]
  -v, --verbose                輸出 DEBUG 日誌
  -h, --help                   顯示此說明
  -V, --version                顯示版本",
        env!("CARGO_PKG_VERSION"),
        DEFAULT_SOCKET_PATH,
        DEFAULT_STATUS_PATH,
        DEFAULT_HOTSPOT_CONF,
        HOTSPOT_SSID
    );
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();

    // 內建假 nmcli 子程序模式（供跨平台測試透過 --nmcli 或 PIWIFI_NMCLI 呼叫）
    if args.first().map(String::as_str) == Some("--fake-nmcli") {
        let Some(state_file) = args.get(1) else {
            eprintln!("missing state file for --fake-nmcli");
            return ExitCode::from(2);
        };
        let nm_args = &args[2..];
        let mut stdin_buf = String::new();
        if nm_args.len() >= 2 && nm_args[0] == "connection" && nm_args[1] == "edit" {
            let _ = std::io::stdin().read_to_string(&mut stdin_buf);
        }
        let input_opt = if stdin_buf.is_empty() {
            None
        } else {
            Some(stdin_buf.as_str())
        };
        let rc = run_fake_nmcli_process(Path::new(state_file), nm_args, input_opt);
        return ExitCode::from(rc as u8);
    }

    let mut subcmd = "daemon".to_string();
    let mut idx = 0;
    if let Some(first) = args.first() {
        match first.as_str() {
            "status" | "scan" | "saved" | "events" | "daemon" => {
                subcmd = first.clone();
                idx = 1;
            }
            "-h" | "--help" => {
                print_help();
                return ExitCode::SUCCESS;
            }
            "-V" | "--version" => {
                println!("piwifi {}", env!("CARGO_PKG_VERSION"));
                return ExitCode::SUCCESS;
            }
            _ => {}
        }
    }

    let mut cfg = WifiConfig {
        iface: env::var("PIWIFI_IFACE")
            .or_else(|_| env::var("PET_WIFI_IFACE"))
            .unwrap_or_else(|_| "wlan0".to_string()),
        observe: env_bool("PIWIFI_OBSERVE"),
        redact_ssid: env_bool("PIWIFI_REDACT_SSID"),
        hotspot_conf: env::var("PIWIFI_HOTSPOT_CONF")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from(DEFAULT_HOTSPOT_CONF)),
        hotspot_ssid: env::var("PIWIFI_HOTSPOT_SSID").unwrap_or_else(|_| HOTSPOT_SSID.to_string()),
        hotspot_url_fallback: env::var("PIWIFI_HOTSPOT_URL")
            .or_else(|_| env::var("PET_HOTSPOT_URL"))
            .unwrap_or_default(),
        pin: env::var("PIWIFI_PIN").ok().filter(|s| !s.is_empty()),
        ..WifiConfig::default()
    };

    let mut socket_path = env::var("PIWIFI_SOCKET")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_SOCKET_PATH));
    let mut status_path = env::var("PIWIFI_STATUS")
        .or_else(|_| env::var("PIWIFI_STATUS_FILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_STATUS_PATH));
    let mut group = env::var("PIWIFI_GROUP").ok().filter(|s| !s.is_empty());
    let mut nmcli_bin = env::var("PIWIFI_NMCLI").unwrap_or_else(|_| "nmcli".to_string());
    let mut interval_secs: f64 = 5.0;
    let mut verbose = false;

    while idx < args.len() {
        match args[idx].as_str() {
            "-h" | "--help" => {
                print_help();
                return ExitCode::SUCCESS;
            }
            "-V" | "--version" => {
                println!("piwifi {}", env!("CARGO_PKG_VERSION"));
                return ExitCode::SUCCESS;
            }
            "--observe" => {
                cfg.observe = true;
                idx += 1;
            }
            "--no-hotspot" => {
                cfg.hotspot_enabled = false;
                idx += 1;
            }
            "--redact-ssid" => {
                cfg.redact_ssid = true;
                idx += 1;
            }
            "-v" | "--verbose" => {
                verbose = true;
                idx += 1;
            }
            "--socket" if idx + 1 < args.len() => {
                socket_path = PathBuf::from(&args[idx + 1]);
                idx += 2;
            }
            "--status" if idx + 1 < args.len() => {
                status_path = PathBuf::from(&args[idx + 1]);
                idx += 2;
            }
            "--group" if idx + 1 < args.len() => {
                group = Some(args[idx + 1].clone());
                idx += 2;
            }
            "--iface" if idx + 1 < args.len() => {
                cfg.iface = args[idx + 1].clone();
                idx += 2;
            }
            "--nmcli" if idx + 1 < args.len() => {
                nmcli_bin = args[idx + 1].clone();
                idx += 2;
            }
            "--hotspot-conf" if idx + 1 < args.len() => {
                cfg.hotspot_conf = PathBuf::from(&args[idx + 1]);
                idx += 2;
            }
            "--hotspot-ssid" if idx + 1 < args.len() => {
                cfg.hotspot_ssid = args[idx + 1].clone();
                idx += 2;
            }
            "--hotspot-url" if idx + 1 < args.len() => {
                cfg.hotspot_url_fallback = args[idx + 1].clone();
                idx += 2;
            }
            "--interval" if idx + 1 < args.len() => {
                interval_secs = args[idx + 1].parse().unwrap_or(5.0);
                idx += 2;
            }
            "--try-secs" if idx + 1 < args.len() => {
                cfg.try_secs = args[idx + 1].parse().unwrap_or(20);
                idx += 2;
            }
            "--rescan-secs" if idx + 1 < args.len() => {
                cfg.rescan_secs = args[idx + 1].parse().unwrap_or(60.0);
                idx += 2;
            }
            "--revert-secs" if idx + 1 < args.len() => {
                cfg.revert_secs = args[idx + 1].parse().unwrap_or(30);
                idx += 2;
            }
            "--hotspot-after" if idx + 1 < args.len() => {
                cfg.hotspot_after = args[idx + 1].parse().unwrap_or(120.0);
                idx += 2;
            }
            "--hotspot-retry" if idx + 1 < args.len() => {
                cfg.hotspot_retry = args[idx + 1].parse().unwrap_or(180.0);
                idx += 2;
            }
            "--pin" if idx + 1 < args.len() => {
                cfg.pin = Some(args[idx + 1].clone());
                idx += 2;
            }
            other => {
                eprintln!("ERROR 未知的參數：{other}");
                return ExitCode::from(2);
            }
        }
    }

    // 除錯 client 子指令
    match subcmd.as_str() {
        "status" | "scan" | "saved" => {
            let req = json!({
                "v": 1,
                "cmd": subcmd,
            });
            match request(&socket_path, &req, Duration::from_secs(30)) {
                Ok(val) => {
                    println!("{}", serde_json::to_string(&val).unwrap_or_default());
                    if val.get("ok").and_then(|v| v.as_bool()) == Some(false) {
                        return ExitCode::from(1);
                    }
                    return ExitCode::SUCCESS;
                }
                Err(msg) => {
                    println!("{}", json!({"ok": false, "error": msg}));
                    return ExitCode::from(1);
                }
            }
        }
        "events" => {
            let res = subscribe_events(&socket_path, None, |ev| {
                println!("{}", serde_json::to_string(&ev).unwrap_or_default());
                true
            });
            if let Err(msg) = res {
                eprintln!("ERROR {msg}");
                return ExitCode::from(1);
            }
            return ExitCode::SUCCESS;
        }
        _ => {}
    }

    init_logger(verbose, cfg.redact_ssid);
    log_info!(
        "piwifi {} 啟動（iface={}，observe={}，nmcli={}）",
        env!("CARGO_PKG_VERSION"),
        cfg.iface,
        cfg.observe,
        nmcli_bin
    );

    let runner = SubprocessRunner::new(nmcli_bin);
    let nm = Arc::new(NmCli::new(runner, cfg.iface.clone(), cfg.observe));
    let mgr = WifiManager::new(nm, cfg);

    let tick_dur = if interval_secs > 0.0 {
        Some(Duration::from_secs_f64(interval_secs))
    } else {
        None
    };

    let _srv = match ServerHandle::start(mgr, &socket_path, Some(status_path), group, tick_dur) {
        Ok(h) => h,
        Err(e) => {
            eprintln!(
                "ERROR 無法啟動 socket 服務（{}）：{}",
                socket_path.display(),
                e
            );
            return ExitCode::from(1);
        }
    };

    loop {
        thread::sleep(Duration::from_secs(3600));
    }
}
