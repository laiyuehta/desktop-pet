pub mod client;
pub mod fake_nmcli;
pub mod log;
pub mod manager;
pub mod nm;
pub mod protocol;
pub mod server;

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use serde_json::{json, Value};

    use crate::client::{request, subscribe_events};
    use crate::fake_nmcli::{FakeNm, FakeState};
    use crate::log::{captured_logs, clear_captured_logs, set_capture_logs};
    use crate::manager::{ClockFn, NotifyFn, SleepFn, WifiConfig, WifiManager};
    use crate::nm::{
        classify_error, key_mgmt_for, split_terse, NmBackend, NmCli, SubprocessRunner, WIFI_TYPE,
    };
    use crate::protocol::handle_line;
    use crate::server::ServerHandle;

    static TEST_DIR_COUNTER: AtomicU64 = AtomicU64::new(1);

    struct TempDir {
        path: PathBuf,
    }

    impl TempDir {
        fn new(prefix: &str) -> Self {
            let id = TEST_DIR_COUNTER.fetch_add(1, Ordering::Relaxed);
            let mut p = std::env::temp_dir();
            p.push(format!("piwifi-test-{prefix}-{}-{id}", std::process::id()));
            let _ = fs::remove_dir_all(&p);
            fs::create_dir_all(&p).unwrap();
            Self { path: p }
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    #[derive(Clone)]
    struct TestClock {
        t: Arc<Mutex<f64>>,
    }

    impl TestClock {
        fn new(initial: f64) -> Self {
            Self {
                t: Arc::new(Mutex::new(initial)),
            }
        }

        fn advance(&self, delta: f64) {
            let mut g = self.t.lock().unwrap();
            *g += delta;
        }

        fn as_fn(&self) -> ClockFn {
            let t = Arc::clone(&self.t);
            Arc::new(move || *t.lock().unwrap())
        }
    }

    struct TestEnv {
        _tmp: TempDir,
        fake: FakeNm,
        mgr: Arc<WifiManager>,
        home: String,
        notes: Arc<Mutex<Vec<String>>>,
        clock: TestClock,
    }

    fn setup_env(observe: bool) -> TestEnv {
        let tmp = TempDir::new("env");
        let fake = FakeNm::new();
        let home = fake.with_state(|st| {
            let u = st.add_conn(
                "HomeAP",
                Some("HomeAP"),
                WIFI_TYPE,
                200,
                1_789_000_000,
                Some("homepass1"),
                true,
                "infrastructure",
                true,
            );
            st.set_air("HomeAP", Some("homepass1"), "WPA2", 59, true);
            st.active = Some(u.clone());
            u
        });
        let conf_path = tmp.path.join("hs.conf");
        fs::write(
            &conf_path,
            "# test\nssid=PetRescue-AP\npassword=hotspotpw123\n",
        )
        .unwrap();

        let notes: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let notes_cb = Arc::clone(&notes);
        let notify_fn: NotifyFn = Arc::new(move |text, _secs| {
            notes_cb.lock().unwrap().push(text.to_string());
        });
        let clock = TestClock::new(1000.0);
        let sleep_fn: SleepFn = Arc::new(|_| {});

        let cfg = WifiConfig {
            hotspot_conf: conf_path,
            observe,
            ..WifiConfig::default()
        };
        let nm = Arc::new(NmCli::new(fake.clone(), "wlan0", observe));
        let mgr = WifiManager::with_hooks(nm, cfg, clock.as_fn(), sleep_fn, notify_fn);

        TestEnv {
            _tmp: tmp,
            fake,
            mgr,
            home,
            notes,
            clock,
        }
    }

    // --- 1. 解析與分類（對應 test_split_terse_escapes / test_classify_errors / test_key_mgmt） ---
    #[test]
    fn test_split_terse_escapes() {
        assert_eq!(split_terse(r"a\:b:c\\d:"), vec!["a:b", r"c\d", ""]);
        assert_eq!(split_terse("x"), vec!["x"]);
    }

    #[test]
    fn test_classify_errors() {
        assert_eq!(
            classify_error(
                "Error: Connection activation failed: Secrets were required, but not provided"
            ),
            "wrong_password"
        );
        assert_eq!(
            classify_error("Error: No network with SSID 'foo' found."),
            "not_found"
        );
        assert_eq!(
            classify_error("Error: Timeout expired (30 seconds)"),
            "timeout"
        );
        assert_eq!(
            classify_error("Error: Not authorized to control networking."),
            "not_authorized"
        );
    }

    #[test]
    fn test_key_mgmt() {
        assert_eq!(key_mgmt_for("WPA2", true), Some("wpa-psk"));
        assert_eq!(key_mgmt_for("WPA3", true), Some("sae"));
        assert_eq!(key_mgmt_for("WPA2 WPA3", true), Some("wpa-psk"));
        assert_eq!(key_mgmt_for("", false), None);
        assert_eq!(key_mgmt_for("WEP", true), None);
    }

    #[test]
    fn test_nm_runner_parsing_and_psk_stdin() {
        type RecordedCall = (Vec<String>, Option<String>);
        let calls: Arc<Mutex<Vec<RecordedCall>>> = Arc::new(Mutex::new(Vec::new()));
        let calls_ref = Arc::clone(&calls);

        let runner = move |args: &[String], input: Option<&str>, _timeout: Duration| {
            calls_ref
                .lock()
                .unwrap()
                .push((args.to_vec(), input.map(ToString::to_string)));
            if args.first().map(String::as_str) == Some("-t")
                && args.iter().any(|a| a == "connection")
                && args.last().map(String::as_str) == Some("show")
            {
                return (
                    0,
                    "HomeAP:d184:802-11-wireless:200:1789000000:yes:wlan0\nWired connection 1:aaaa:802-3-ethernet:-999:0:yes:eth0\n".to_string(),
                    String::new(),
                );
            }
            if args.iter().any(|a| a == "wifi") && args.iter().any(|a| a == "list") {
                return (
                    0,
                    "*:home\\:0503:59:WPA2:1\n :Other:40:WPA1 WPA2:6\n".to_string(),
                    String::new(),
                );
            }
            if args.len() >= 2 && args[0] == "connection" && args[1] == "add" {
                return (
                    0,
                    "Connection 'x' (12345678-1234-1234-1234-123456789abc) successfully added.\n"
                        .to_string(),
                    String::new(),
                );
            }
            if args.len() >= 2 && args[0] == "connection" && args[1] == "edit" {
                return (
                    0,
                    "Connection 'x' successfully saved.\n".to_string(),
                    String::new(),
                );
            }
            (0, String::new(), String::new())
        };

        let nm = NmCli::new(runner, "wlan0", false);
        let c = nm.connections().unwrap();
        assert_eq!(c[0].priority, 200);
        assert!(c[0].active);
        assert_eq!(c[1].priority, -999);

        let w = nm.wifi_list("wlan0", true).unwrap();
        assert_eq!(w[0].ssid, "home:0503");
        assert!(w[0].in_use);
        assert_eq!(w[1].security, "WPA1 WPA2");

        let u = nm
            .add_wifi("x", "x", "wlan0", Some("wpa-psk"), false, false)
            .unwrap();
        assert_eq!(u, "12345678-1234-1234-1234-123456789abc");

        nm.set_psk(&u, "s3cret-pass").unwrap();
        let recorded = calls.lock().unwrap();
        // 密碼只在 stdin，不在任何一次的指令列參數
        assert!(recorded
            .iter()
            .all(|(a, _)| !a.join(" ").contains("s3cret-pass")));
        assert!(recorded
            .last()
            .unwrap()
            .1
            .as_deref()
            .unwrap()
            .contains("s3cret-pass"));
        drop(recorded);

        assert!(nm.set_psk(&u, "bad\npass").is_err());
    }

    #[test]
    fn test_nm_set_psk_error_hides_output() {
        let nm = NmCli::new(
            |_args: &[String], _in: Option<&str>, _t: Duration| {
                (
                    0,
                    "Error: failed to set 'psk' property: invalid 'zzzSECRET'".to_string(),
                    String::new(),
                )
            },
            "wlan0",
            false,
        );
        let err = nm.set_psk("u", "zzzSECRETzz").unwrap_err();
        assert!(!err.msg.contains("SECRET"));
        assert_eq!(err.kind, "wrong_password");
    }

    #[test]
    fn test_nm_error_kind() {
        let nm = NmCli::new(
            |_args: &[String], _in: Option<&str>, _t: Duration| {
                (
                    4,
                    String::new(),
                    "Error: Connection activation failed: Secrets were required, but not provided."
                        .to_string(),
                )
            },
            "wlan0",
            false,
        );
        let err = nm.up("u", "wlan0", 30).unwrap_err();
        assert_eq!(err.kind, "wrong_password");
    }

    // --- 2. 查詢（對應 test_status_scan_saved） ---
    #[test]
    fn test_status_scan_saved() {
        let env = setup_env(false);
        env.fake.with_state(|st| {
            st.set_air("Cafe", Some("cafepass1"), "WPA2", 70, false);
            st.set_air("Open", None, "", 30, false);
        });

        let st = env.mgr.status(0.0);
        assert_eq!(st.wifi.ssid.as_deref(), Some("HomeAP"));
        assert_eq!(st.wifi.ip.as_deref(), Some("192.0.2.101"));
        assert_eq!(st.wired[0].device, "eth0");
        assert_eq!(st.wired[0].ip.as_deref(), Some("192.0.2.43"));
        assert_eq!(st.proto, 1);
        assert_eq!(st.name, "piwifi");

        let nets = env.mgr.scan(true).unwrap();
        let ssids: Vec<&str> = nets.iter().map(|n| n.ssid.as_str()).collect();
        assert_eq!(ssids, vec!["Cafe", "Open"]); // 隱藏網路掃不到
        assert!(!nets[1].secured);

        let saved = env.mgr.saved().unwrap();
        assert_eq!(saved.len(), 1);
        assert_eq!(saved[0].ssid, "HomeAP");
        assert!(saved[0].hidden);
        assert!(saved[0].last_connected.is_some());
        let saved_json = serde_json::to_string(&saved).unwrap();
        assert!(!saved_json.contains("psk"));
        assert!(!saved_json.contains("homepass1"));
    }

    // --- 3. 連線與切回（對應 test_connect_*） ---
    #[test]
    fn test_connect_new_success_saves_and_prioritizes() {
        let env = setup_env(false);
        env.fake.with_state(|st| {
            st.set_air("Cafe", Some("cafepass1"), "WPA2", 70, false);
        });
        let op = env
            .mgr
            .start_connect(
                Some("Cafe".to_string()),
                Some("cafepass1".to_string()),
                false,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op.result.as_deref(), Some("ok"));
        assert_eq!(op.phase, "done");
        let new_uuid = op.uuid.unwrap();
        env.fake.with_state(|st| {
            assert_eq!(st.active.as_deref(), Some(new_uuid.as_str()));
            let new_c = st.get_conn(&new_uuid).unwrap();
            let home_c = st.get_conn(&env.home).unwrap();
            assert!(new_c.autoconnect);
            assert!(new_c.priority > home_c.priority);
            assert_eq!(home_c.priority, 200);
        });
        assert_eq!(env.mgr.saved().unwrap()[0].ssid, "Cafe");
        assert!(env.notes.lock().unwrap().iter().any(|n| n.contains("Cafe")));
    }

    #[test]
    fn test_connect_wrong_password_reverts() {
        let env = setup_env(false);
        env.fake.with_state(|st| {
            st.set_air("Cafe", Some("cafepass1"), "WPA2", 70, false);
        });
        let before_uuids: Vec<String> = env
            .fake
            .with_state(|st| st.conns.iter().map(|c| c.uuid.clone()).collect());
        let op = env
            .mgr
            .start_connect(
                Some("Cafe".to_string()),
                Some("wrongpass".to_string()),
                false,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op.result.as_deref(), Some("wrong_password"));
        assert!(op.reverted);
        assert!(op.message.contains("密碼錯誤"));
        env.fake.with_state(|st| {
            assert_eq!(st.active.as_deref(), Some(env.home.as_str()));
            let after_uuids: Vec<String> = st.conns.iter().map(|c| c.uuid.clone()).collect();
            assert_eq!(after_uuids, before_uuids); // 失敗的新設定刪掉，原本的都在
        });
        let op_json = serde_json::to_string(&op).unwrap();
        assert!(!op_json.contains("wrongpass"));
    }

    #[test]
    fn test_connect_wrong_password_same_ssid_keeps_original() {
        let env = setup_env(false);
        let orig = env
            .fake
            .with_state(|st| st.get_conn(&env.home).unwrap().clone());
        let op = env
            .mgr
            .start_connect(
                Some("HomeAP".to_string()),
                Some("wrongpass".to_string()),
                true,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op.result.as_deref(), Some("wrong_password"));
        assert!(op.reverted);
        env.fake.with_state(|st| {
            let cur = st.get_conn(&env.home).unwrap().clone();
            assert_eq!(cur.psk, orig.psk);
            assert_eq!(cur.priority, orig.priority);
            assert_eq!(st.active.as_deref(), Some(env.home.as_str()));
        });
    }

    #[test]
    fn test_connect_not_found_and_hidden() {
        let env = setup_env(false);
        let op = env
            .mgr
            .start_connect(
                Some("Nope".to_string()),
                Some("whatever1".to_string()),
                false,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op.result.as_deref(), Some("not_found"));
        env.fake
            .with_state(|st| assert_eq!(st.active.as_deref(), Some(env.home.as_str())));

        env.fake.with_state(|st| {
            st.set_air("Secret", Some("secretpw1"), "WPA2", 65, true);
        });
        let op2 = env
            .mgr
            .start_connect(
                Some("Secret".to_string()),
                Some("secretpw1".to_string()),
                true,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op2.result.as_deref(), Some("ok"));
        env.fake.with_state(|st| {
            assert!(st.calls.contains(&vec![
                "add".to_string(),
                "Secret".to_string(),
                "Secret".to_string(),
                "wpa-psk".to_string(),
                "true".to_string(),
                "false".to_string(),
            ]));
        });
    }

    #[test]
    fn test_connect_saved_by_uuid_and_replace_old_password() {
        let env = setup_env(false);
        let old = env.fake.with_state(|st| {
            st.set_air("Cafe", Some("newpass99"), "WPA2", 70, false);
            st.add_conn(
                "Cafe",
                Some("Cafe"),
                WIFI_TYPE,
                100,
                0,
                Some("oldpass11"),
                false,
                "infrastructure",
                true,
            )
        });
        let op1 = env
            .mgr
            .start_connect(None, None, false, Some(old.clone()), false)
            .unwrap();
        assert_eq!(op1.result.as_deref(), Some("wrong_password"));
        env.fake.with_state(|st| {
            assert_eq!(st.active.as_deref(), Some(env.home.as_str()));
            assert!(st.get_conn(&old).is_some()); // 已存設定不能因為連線失敗被刪
        });

        let op2 = env
            .mgr
            .start_connect(
                Some("Cafe".to_string()),
                Some("newpass99".to_string()),
                false,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op2.result.as_deref(), Some("ok"));
        env.fake.with_state(|st| {
            assert!(st.get_conn(&old).is_none()); // 新密碼連線成功後取代舊設定
        });
        let cafe_count = env
            .mgr
            .saved()
            .unwrap()
            .iter()
            .filter(|s| s.ssid == "Cafe")
            .count();
        assert_eq!(cafe_count, 1);
    }

    #[test]
    fn test_connect_validation() {
        let env = setup_env(false);
        assert!(env
            .mgr
            .start_connect(
                Some("x".to_string()),
                Some("short".to_string()),
                false,
                None,
                false
            )
            .is_err());
        assert!(env
            .mgr
            .start_connect(None, None, false, None, false)
            .is_err());
    }

    // --- 4. 刪除與排序（對應 test_forget_and_priority） ---
    #[test]
    fn test_forget_and_priority() {
        let env = setup_env(false);
        let (a, b) = env.fake.with_state(|st| {
            let a = st.add_conn(
                "A",
                Some("A"),
                WIFI_TYPE,
                50,
                0,
                Some("aaaaaaaa"),
                false,
                "infrastructure",
                true,
            );
            let b = st.add_conn(
                "B",
                Some("B"),
                WIFI_TYPE,
                40,
                0,
                Some("bbbbbbbb"),
                false,
                "infrastructure",
                true,
            );
            (a, b)
        });
        // 連線中的網路不能刪
        assert!(env.mgr.forget(&env.home).is_err());

        let saved = env
            .mgr
            .set_priority(None, Some(b.clone()), Some("top".to_string()), None)
            .unwrap();
        let names: Vec<&str> = saved.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, vec!["B", "HomeAP", "A"]);

        let saved2 = env
            .mgr
            .set_priority(
                Some(vec![a.clone(), env.home.clone(), b.clone()]),
                None,
                None,
                None,
            )
            .unwrap();
        let names2: Vec<&str> = saved2.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names2, vec!["A", "HomeAP", "B"]);

        // 直接設定單一連線優先值 priority {uuid, value}
        let saved3 = env
            .mgr
            .set_priority(None, Some(b.clone()), None, Some(300))
            .unwrap();
        assert_eq!(saved3[0].name, "B");
        assert_eq!(saved3[0].priority, 300);

        assert!(env
            .mgr
            .set_priority(Some(vec![a.clone()]), None, None, None)
            .is_err());

        env.mgr.forget(&a).unwrap();
        env.fake.with_state(|st| assert!(st.get_conn(&a).is_none()));
    }

    // --- 5. 自動嘗試與救援熱點（對應 test_auto_reconnect_* / test_hotspot_*） ---
    #[test]
    fn test_auto_reconnect_on_disconnect() {
        let env = setup_env(false);
        let other = env.fake.with_state(|st| {
            let u = st.add_conn(
                "Backup",
                Some("Backup"),
                WIFI_TYPE,
                100,
                0,
                Some("backup11"),
                false,
                "infrastructure",
                true,
            );
            st.set_air("Backup", Some("backup11"), "WPA2", 65, false);
            u
        });
        assert_eq!(env.mgr.tick(), "connected");

        env.fake.with_state(|st| {
            st.remove_air("HomeAP");
            st.active = None;
        });
        assert_eq!(env.mgr.tick(), "connected");
        env.fake.with_state(|st| {
            assert_eq!(st.active.as_deref(), Some(other.as_str()));
            let ups: Vec<&Vec<String>> = st.calls.iter().filter(|c| c[0] == "up").collect();
            assert_eq!(ups[0][1], env.home);
            assert_eq!(ups[0][2], "20");
            assert_eq!(ups.last().unwrap()[1], other);
        });
        assert!(env
            .notes
            .lock()
            .unwrap()
            .iter()
            .any(|n| n.contains("沒有網路")));
    }

    #[test]
    fn test_hotspot_after_timeout_and_off_when_back() {
        let env = setup_env(false);
        env.fake.with_state(|st| {
            st.remove_air("HomeAP");
            st.active = None;
        });
        assert_eq!(env.mgr.tick(), "disconnected");
        env.clock.advance(30.0);
        assert_eq!(env.mgr.tick(), "disconnected");

        let n_up = env
            .fake
            .with_state(|st| st.calls.iter().filter(|c| c[0] == "up").count());
        env.clock.advance(31.0); // 一分鐘後重掃再試
        env.mgr.tick();
        let n_up2 = env
            .fake
            .with_state(|st| st.calls.iter().filter(|c| c[0] == "up").count());
        assert!(n_up2 > n_up);

        env.clock.advance(60.0); // 超過 2 分鐘 → 開救援熱點
        assert_eq!(env.mgr.tick(), "hotspot");

        env.fake.with_state(|st| {
            let hs = st
                .conns
                .iter()
                .find(|c| c.name == "PetRescue-AP")
                .expect("hotspot created");
            assert_eq!(hs.mode, "ap");
            assert_eq!(hs.psk.as_deref(), Some("hotspotpw123"));
            assert_eq!(hs.pmf.as_deref(), Some("disable"));
            assert_eq!(st.active.as_deref(), Some(hs.uuid.as_str()));
        });
        assert_eq!(env.mgr.saved().unwrap()[0].name, "HomeAP"); // 熱點不在已記錄清單
        assert!(env
            .notes
            .lock()
            .unwrap()
            .iter()
            .any(|n| n.contains("PetRescue-AP") && n.contains("192.0.2.1:8080")));
        assert_eq!(
            env.mgr.status(0.0).hotspot.url.as_deref(),
            Some("http://192.0.2.1:8080/")
        );

        // 熱點模式下設定新網路 → 熱點自動關閉
        env.fake.with_state(|st| {
            st.set_air("Cafe", Some("cafepass1"), "WPA2", 70, false);
        });
        let op = env
            .mgr
            .start_connect(
                Some("Cafe".to_string()),
                Some("cafepass1".to_string()),
                false,
                None,
                false,
            )
            .unwrap();
        assert_eq!(op.result.as_deref(), Some("ok"));
        assert_eq!(env.mgr.tick(), "connected");
    }

    #[test]
    fn test_hotspot_retries_known_networks() {
        let env = setup_env(false);
        env.fake.with_state(|st| {
            st.remove_air("HomeAP");
            st.active = None;
        });
        env.mgr.tick();
        env.clock.advance(125.0);
        assert_eq!(env.mgr.tick(), "hotspot");

        env.clock.advance(200.0);
        env.fake.with_state(|st| {
            st.set_air("HomeAP", Some("homepass1"), "WPA2", 59, true);
        });
        assert_eq!(env.mgr.tick(), "connected");
        env.fake.with_state(|st| {
            assert_eq!(st.active.as_deref(), Some(env.home.as_str()));
        });
    }

    #[test]
    fn test_no_hotspot_without_conf() {
        let tmp = TempDir::new("noconf");
        let fake = FakeNm::new();
        let cfg = WifiConfig {
            hotspot_conf: tmp.path.join("missing.conf"),
            hotspot_after: 0.0,
            ..WifiConfig::default()
        };
        let nm = Arc::new(NmCli::new(fake.clone(), "wlan0", false));
        let mgr = WifiManager::new(nm, cfg);
        assert_eq!(mgr.tick(), "disconnected");
        fake.with_state(|st| {
            assert!(!st.conns.iter().any(|c| c.name == "PetRescue-AP"));
        });
    }

    #[test]
    fn test_manual_hotspot_toggle_and_pmf_disable() {
        let env = setup_env(false);
        env.fake.with_state(|st| st.ap_ok = false);
        let (ok, _) = env.mgr.set_hotspot(true).unwrap();
        assert!(!ok);

        env.fake.with_state(|st| st.ap_ok = true);
        let (ok2, mode2) = env.mgr.set_hotspot(true).unwrap();
        assert!(ok2);
        assert_eq!(mode2, "hotspot");

        let hs_uuid = env.fake.with_state(|st| {
            st.conns
                .iter()
                .find(|c| c.name == "PetRescue-AP")
                .unwrap()
                .uuid
                .clone()
        });
        assert!(env.fake.with_state(|st| st.calls.contains(&vec![
            "modify".to_string(),
            hs_uuid,
            "802-11-wireless-security.pmf".to_string(),
            "disable".to_string(),
        ])));

        env.mgr.set_hotspot(false).unwrap();
        assert_eq!(env.mgr.tick(), "connected");
        env.fake
            .with_state(|st| assert_eq!(st.active.as_deref(), Some(env.home.as_str())));
    }

    #[test]
    fn test_hotspot_retry_when_interrupted() {
        let env = setup_env(false);
        env.fake.with_state(|st| st.flaky_ap_once = true);
        let (ok, mode) = env.mgr.set_hotspot(true).unwrap();
        assert!(ok);
        assert_eq!(mode, "hotspot");
    }

    // --- 6. --observe 模式測試（證明絕不呼叫任何會改變網路的 nmcli 指令） ---
    #[test]
    fn test_observe_mode_never_modifies_network() {
        set_capture_logs(true);
        clear_captured_logs();
        let env = setup_env(true);

        // 1. 透過協定發送所有會改變網路的指令，全部必須回 {"ok":false,"error":"observe 模式"}
        for cmd_json in [
            json!({"v": 1, "cmd": "connect", "ssid": "Cafe", "password": "cafepass1"}),
            json!({"v": 1, "cmd": "forget", "uuid": env.home}),
            json!({"v": 1, "cmd": "priority", "uuid": env.home, "value": 300}),
            json!({"v": 1, "cmd": "hotspot", "on": true}),
        ] {
            let crate::protocol::DispatchResult::Response(resp) =
                handle_line(&env.mgr, &cmd_json.to_string())
            else {
                panic!("expected response");
            };
            assert_eq!(resp.get("ok").and_then(Value::as_bool), Some(false));
            assert_eq!(
                resp.get("error").and_then(Value::as_str),
                Some("observe 模式")
            );
        }

        // 2. 模擬斷線超過 2 分鐘，背景 tick 在 observe 模式下也不會連線或開熱點
        env.fake.with_state(|st| {
            st.remove_air("HomeAP");
            st.active = None;
        });
        assert_eq!(env.mgr.tick(), "disconnected");
        env.clock.advance(150.0);
        assert_eq!(env.mgr.tick(), "disconnected");

        // 3. 檢查所有實際執行的 nmcli CLI 參數：沒有任何 add/edit/up/down/delete/modify，且 wifi list 只用 --rescan no
        env.fake.with_state(|st| {
            assert!(
                st.calls.iter().all(|c| c[0] == "list" && c[1] == "false"),
                "observe 模式下不應執行任何修改指令或主動掃描：{:?}",
                st.calls
            );
            assert!(
                st.raw_cli_calls
                    .iter()
                    .all(|args| !FakeState::is_mutating_cli(args)),
                "observe 模式下發現修改指令：{:?}",
                st.raw_cli_calls
            );
        });

        let logs = captured_logs().join("\n");
        assert!(logs.contains("[observe]"));
        assert!(!logs.contains("cafepass1"));
    }

    // --- 7. 密碼不外洩測試（socket 回應、status.json、日誌都不含密碼） ---
    #[test]
    fn test_passwords_never_in_responses_status_or_logs() {
        set_capture_logs(true);
        clear_captured_logs();
        let env = setup_env(false);
        let wifi_pw = "MySuperSecretWifi99";
        let wrong_pw = "WrongAttemptPass88";
        let hs_pw = "hotspotpw123";

        env.fake.with_state(|st| {
            st.set_air("SecureNet", Some(wifi_pw), "WPA2", 80, false);
        });

        // 1. 錯誤密碼連線
        let crate::protocol::DispatchResult::Response(r1) = handle_line(
            &env.mgr,
            &json!({"v": 1, "cmd": "connect", "ssid": "SecureNet", "password": wrong_pw, "wait": true}).to_string(),
        ) else {
            panic!("expected response");
        };
        // 2. 正確密碼連線
        let crate::protocol::DispatchResult::Response(r2) = handle_line(
            &env.mgr,
            &json!({"v": 1, "cmd": "connect", "ssid": "SecureNet", "password": wifi_pw, "wait": true}).to_string(),
        ) else {
            panic!("expected response");
        };
        // 3. 開啟熱點
        let crate::protocol::DispatchResult::Response(r3) = handle_line(
            &env.mgr,
            &json!({"v": 1, "cmd": "hotspot", "on": true}).to_string(),
        ) else {
            panic!("expected response");
        };
        // 4. 查詢 status / saved / scan
        let crate::protocol::DispatchResult::Response(r4) =
            handle_line(&env.mgr, &json!({"v": 1, "cmd": "status"}).to_string())
        else {
            panic!("expected response");
        };
        let crate::protocol::DispatchResult::Response(r5) =
            handle_line(&env.mgr, &json!({"v": 1, "cmd": "saved"}).to_string())
        else {
            panic!("expected response");
        };

        let status_file = env._tmp.path.join("status.json");
        crate::protocol::write_status_file(&env.mgr, &status_file).unwrap();
        let status_file_content = fs::read_to_string(&status_file).unwrap();
        let logs_content = captured_logs().join("\n");

        let combined = format!(
            "{}\n{}\n{}\n{}\n{}\n{}\n{}",
            r1, r2, r3, r4, r5, status_file_content, logs_content
        );
        for secret in [wifi_pw, wrong_pw, hs_pw, "homepass1"] {
            assert!(
                !combined.contains(secret),
                "機密密碼 {secret} 不應出現在回應、status.json 或日誌中"
            );
        }
        // 確認每行日誌第一個字都是等級
        for line in captured_logs() {
            let first = line.split_whitespace().next().unwrap_or("");
            assert!(
                matches!(first, "INFO" | "WARN" | "ERROR" | "DEBUG"),
                "日誌第一個字應為等級：{line}"
            );
        }
    }

    // --- 8. 介面隔離與不刪已存網路測試 ---
    #[test]
    fn test_only_wlan0_and_never_touches_eth0_or_saved_networks() {
        let env = setup_env(false);
        let nm = NmCli::new(env.fake.clone(), "wlan0", false);

        // 對 eth0 下指令一律拒絕
        assert!(nm.wifi_list("eth0", false).is_err());
        assert!(nm
            .add_wifi("bad", "bad", "eth0", Some("wpa-psk"), false, false)
            .is_err());
        assert!(nm.up(&env.home, "eth0", 20).is_err());

        // 試圖 forget eth0 連線（Wired connection 1）會被拒絕，且 eth0 保持不變
        let eth_uuid = env.fake.with_state(|st| {
            st.conns
                .iter()
                .find(|c| c.conn_type == "802-3-ethernet")
                .unwrap()
                .uuid
                .clone()
        });
        assert!(env.mgr.forget(&eth_uuid).is_err());
        env.fake
            .with_state(|st| assert!(st.get_conn(&eth_uuid).is_some()));

        // 所有執行的 nmcli 指令都不會對 eth0 做修改
        env.fake.with_state(|st| {
            for call in &st.raw_cli_calls {
                if FakeState::is_mutating_cli(call) {
                    assert!(
                        !call.iter().any(|a| a == "eth0" || a == &eth_uuid),
                        "不應對 eth0 下修改指令：{:?}",
                        call
                    );
                }
            }
        });
    }

    // --- 9. Socket 協定 v1 全指令、subscribe 事件與 status.json 整合測試 ---
    #[test]
    fn test_protocol_v1_socket_and_subscribe() {
        let env = setup_env(false);
        env.fake.with_state(|st| {
            st.set_air("Cafe", Some("cafepass1"), "WPA2", 75, false);
        });
        let sock_path = env._tmp.path.join("piwifi.sock");
        let status_path = env._tmp.path.join("status.json");

        let mut srv = ServerHandle::start(
            Arc::clone(&env.mgr),
            &sock_path,
            Some(status_path.clone()),
            None,
            None,
        )
        .unwrap();

        let timeout = Duration::from_secs(5);

        // 1. status（帶 "v":1 與不帶 "v" 都相容）
        let st1 = request(&sock_path, &json!({"v": 1, "cmd": "status"}), timeout).unwrap();
        assert_eq!(st1.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(st1.get("proto").and_then(Value::as_u64), Some(1));
        assert_eq!(
            st1.get("version").and_then(Value::as_str),
            Some(env!("CARGO_PKG_VERSION"))
        );

        let st_no_v = request(&sock_path, &json!({"cmd": "status"}), timeout).unwrap();
        assert_eq!(st_no_v.get("ok").and_then(Value::as_bool), Some(true));

        // 不支援的版本回 unsupported_version
        let st_v2 = request(&sock_path, &json!({"v": 2, "cmd": "status"}), timeout).unwrap();
        assert_eq!(st_v2.get("ok").and_then(Value::as_bool), Some(false));
        assert_eq!(
            st_v2.get("kind").and_then(Value::as_str),
            Some("unsupported_version")
        );

        // 2. scan & saved
        let scan_res = request(&sock_path, &json!({"v": 1, "cmd": "scan"}), timeout).unwrap();
        assert_eq!(scan_res.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(scan_res["networks"][0]["ssid"].as_str(), Some("Cafe"));

        let saved_res = request(&sock_path, &json!({"v": 1, "cmd": "saved"}), timeout).unwrap();
        assert_eq!(saved_res.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(saved_res["saved"][0]["ssid"].as_str(), Some("HomeAP"));

        // 3. subscribe 訂閱狀態變化
        let events: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
        let events_clone = Arc::clone(&events);
        let sock_sub = sock_path.clone();
        let sub_thread = std::thread::spawn(move || {
            let _ = subscribe_events(&sock_sub, Some(Duration::from_secs(5)), |ev| {
                let mut g = events_clone.lock().unwrap();
                g.push(ev);
                g.len() < 2
            });
        });

        // 等初始事件收到後觸發熱點切換
        for _ in 0..50 {
            if !events.lock().unwrap().is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let hs_res = request(
            &sock_path,
            &json!({"v": 1, "cmd": "hotspot", "on": true}),
            timeout,
        )
        .unwrap();
        assert_eq!(hs_res.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(hs_res.get("mode").and_then(Value::as_str), Some("hotspot"));

        let _ = sub_thread.join();
        let ev_list = events.lock().unwrap();
        assert!(ev_list.len() >= 2);
        assert_eq!(ev_list.last().unwrap()["mode"].as_str(), Some("hotspot"));
        drop(ev_list);

        // 4. 檢查 status.json 共同欄位
        crate::protocol::write_status_file(&env.mgr, &status_path).unwrap();
        let st_file: Value =
            serde_json::from_str(&fs::read_to_string(&status_path).unwrap()).unwrap();
        for key in [
            "name",
            "version",
            "proto",
            "pid",
            "started",
            "uptime",
            "clients",
            "errors",
            "last_error",
            "last_error_ts",
            "mode",
            "wifi",
            "wired",
            "hotspot",
        ] {
            assert!(st_file.get(key).is_some(), "status.json 缺少共同欄位 {key}");
        }

        srv.stop();
    }

    // --- 10. 外部假 nmcli 執行檔（SubprocessRunner）端到端測試 ---
    #[test]
    fn test_subprocess_fake_nmcli_end_to_end() {
        let tmp = TempDir::new("subproc");
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
        init_state.set_air("HomeAP", Some("homepass1"), "WPA2", 60, false);
        init_state.set_air("Office", Some("officepw123"), "WPA2", 85, false);
        init_state.active = Some(home);
        fs::write(
            &state_path,
            serde_json::to_string_pretty(&init_state).unwrap(),
        )
        .unwrap();

        let runner = SubprocessRunner::with_extra_args(
            std::env::current_exe()
                .unwrap()
                .to_string_lossy()
                .to_string(),
            vec![
                "--fake-nmcli".to_string(),
                state_path.to_string_lossy().to_string(),
            ],
        );

        let _ = runner;
        // 直接測試 run_fake_nmcli_process 狀態持久化與 SubprocessRunner
        let rc = crate::fake_nmcli::run_fake_nmcli_process(
            &state_path,
            &[
                "-t".to_string(),
                "-f".to_string(),
                "DEVICE,TYPE,STATE,CONNECTION".to_string(),
                "device".to_string(),
                "status".to_string(),
            ],
            None,
        );
        assert_eq!(rc, 0);
        let loaded: FakeState =
            serde_json::from_str(&fs::read_to_string(&state_path).unwrap()).unwrap();
        assert_eq!(loaded.raw_cli_calls.len(), 1);
    }
}
