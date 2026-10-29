use std::sync::{Mutex, OnceLock};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum LogLevel {
    Debug = 0,
    Info = 1,
    Warn = 2,
    Error = 3,
}

impl LogLevel {
    pub fn as_str(self) -> &'static str {
        match self {
            LogLevel::Debug => "DEBUG",
            LogLevel::Info => "INFO",
            LogLevel::Warn => "WARN",
            LogLevel::Error => "ERROR",
        }
    }
}

struct LoggerState {
    min_level: LogLevel,
    redact_ssid: bool,
    capture: bool,
    buffer: Vec<String>,
}

static LOGGER: OnceLock<Mutex<LoggerState>> = OnceLock::new();

fn state() -> &'static Mutex<LoggerState> {
    LOGGER.get_or_init(|| {
        Mutex::new(LoggerState {
            min_level: LogLevel::Info,
            redact_ssid: false,
            capture: cfg!(test),
            buffer: Vec::new(),
        })
    })
}

pub fn init_logger(verbose: bool, redact_ssid: bool) {
    if let Ok(mut st) = state().lock() {
        st.min_level = if verbose {
            LogLevel::Debug
        } else {
            LogLevel::Info
        };
        st.redact_ssid = redact_ssid;
    }
}

pub fn set_capture_logs(capture: bool) {
    if let Ok(mut st) = state().lock() {
        st.capture = capture;
    }
}

pub fn clear_captured_logs() {
    if let Ok(mut st) = state().lock() {
        st.buffer.clear();
    }
}

pub fn captured_logs() -> Vec<String> {
    if let Ok(st) = state().lock() {
        st.buffer.clone()
    } else {
        Vec::new()
    }
}

pub fn fmt_ssid(ssid: &str, force_redact: bool) -> String {
    let global_redact = state().lock().map(|s| s.redact_ssid).unwrap_or(false);
    if force_redact || global_redact {
        "***".to_string()
    } else {
        ssid.to_string()
    }
}

/// 輸出單行日誌到 stdout/stderr（交給 journald），第一個字一定是等級（INFO / WARN / ERROR / DEBUG）。
pub fn log_line(level: LogLevel, msg: &str) {
    let single_line = msg.replace(['\r', '\n'], " ");
    let formatted = format!("{} {}", level.as_str(), single_line.trim());
    if let Ok(mut st) = state().lock() {
        if st.capture {
            if st.buffer.len() >= 1000 {
                st.buffer.remove(0);
            }
            st.buffer.push(formatted.clone());
        }
        if level < st.min_level {
            return;
        }
    }
    match level {
        LogLevel::Error | LogLevel::Warn => eprintln!("{formatted}"),
        LogLevel::Info | LogLevel::Debug => println!("{formatted}"),
    }
}

#[macro_export]
macro_rules! log_debug {
    ($($arg:tt)*) => {
        $crate::log::log_line($crate::log::LogLevel::Debug, &format!($($arg)*))
    };
}

#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => {
        $crate::log::log_line($crate::log::LogLevel::Info, &format!($($arg)*))
    };
}

#[macro_export]
macro_rules! log_warn {
    ($($arg:tt)*) => {
        $crate::log::log_line($crate::log::LogLevel::Warn, &format!($($arg)*))
    };
}

#[macro_export]
macro_rules! log_error {
    ($($arg:tt)*) => {
        $crate::log::log_line($crate::log::LogLevel::Error, &format!($($arg)*))
    };
}
