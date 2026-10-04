//! Gaeclip Desktop shell (Tauri 2) — scaffold.
//!
//! Spawns two sidecars with GAECLIP_RUNTIME=desktop and a shared
//! STORAGE_ROOT under %LOCALAPPDATA%\\Gaeclip:
//!   1. `gaeclip-api`  (PyInstaller bundle: FastAPI + in-process LocalRunner)
//!   2. bundled `node server.js` (Next.js standalone frontend on 3000)
//! The main window loads the local frontend.
//!
//! Requires `tauri-plugin-shell` (sidecars). First real validation happens
//! on Windows CI (.github/workflows/windows-build.yml); this file has not
//! been compiled on Linux (no Rust toolchain here).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use tauri::path::BaseDirectory;
use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

fn app_root(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .app_local_data_dir()
        .unwrap_or_else(|_| std::path::PathBuf::from("."))
        .join("Gaeclip")
}

fn log_line(log: &Path, msg: &str) {
    use std::io::Write as _;
    let line = format!("[{:?}] {}\n", std::time::SystemTime::now(), msg);
    if let Some(parent) = log.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
    {
        let _ = file.write_all(line.as_bytes());
    }
    eprintln!("{msg}");
}

fn log_dir(log: &Path, label: &str, dir: &Path) {
    log_line(
        log,
        &format!("{label} = {} (exists={})", dir.display(), dir.is_dir()),
    );
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) => {
            log_line(log, &format!("{label} list failed: {e}"));
            return;
        }
    };
    let mut count = 0;
    for entry in entries.flatten() {
        let kind = if entry.path().is_dir() { "dir" } else { "file" };
        log_line(
            log,
            &format!("{label} .. {kind} {}", entry.file_name().to_string_lossy()),
        );
        count += 1;
        if count >= 60 {
            log_line(log, &format!("{label} .. (truncated at 60)"));
            break;
        }
    }
}

fn sidecar_env(storage: &Path) -> HashMap<String, String> {
    let _ = std::fs::create_dir_all(storage);
    let mut env = HashMap::new();
    env.insert("GAECLIP_RUNTIME".to_string(), "desktop".to_string());
    env.insert(
        "STORAGE_ROOT".to_string(),
        storage.to_string_lossy().into_owned(),
    );
    env.insert("API_HOST".to_string(), "127.0.0.1".to_string());
    env.insert("API_PORT".to_string(), "8000".to_string());
    env.insert(
        "NEXT_PUBLIC_API_BASE_URL".to_string(),
        "http://127.0.0.1:8000".to_string(),
    );
    env
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let root = app_root(app.handle());
            let log = root.join("desktop-startup.log");
            // Fresh log per launch so the last lines always describe the
            // failing run. Release builds hide the console
            // (windows_subsystem), so without this file a startup failure
            // is a silent force-close with zero diagnostics.
            let _ = std::fs::write(&log, "");
            log_line(&log, "setup start");

            let storage = root.join("storage");
            if let Err(e) = std::fs::create_dir_all(&storage) {
                let msg = format!(
                    "FATAL cannot create storage dir {}: {e}",
                    storage.display()
                );
                log_line(&log, &msg);
                return Err(e.into());
            }
            let env = sidecar_env(&storage);

            // 1. Backend API (LocalRunner inside; no Redis/Celery/Docker).
            log_line(&log, "resolving gaeclip-api sidecar...");
            let (mut rx_api, _child_api) = app
                .shell()
                .sidecar("gaeclip-api")
                .map_err(|e| {
                    log_line(&log, &format!("FATAL api sidecar missing: {e}"));
                    e
                })?
                .envs(env.clone())
                .spawn()
                .map_err(|e| {
                    log_line(&log, &format!("FATAL failed to spawn api sidecar: {e}"));
                    e
                })?;
            log_line(&log, "gaeclip-api spawned");
            let log_api = log.clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx_api.recv().await {
                    match event {
                        CommandEvent::Stderr(line) => log_line(
                            &log_api,
                            &format!("[api] {}", String::from_utf8_lossy(&line)),
                        ),
                        CommandEvent::Stdout(line) => log_line(
                            &log_api,
                            &format!("[api] {}", String::from_utf8_lossy(&line)),
                        ),
                        CommandEvent::Terminated(payload) => log_line(
                            &log_api,
                            &format!("[api] process terminated: {payload:?}"),
                        ),
                        _ => {}
                    }
                }
            });

            // 2. Frontend: Next.js standalone served by bundled node.
            log_line(&log, "resolving frontend resources...");
            let node = app
                .path()
                .resolve("bin/node.exe", BaseDirectory::Resource)
                .map_err(|e| {
                    log_line(&log, &format!("FATAL node resource missing: {e}"));
                    e
                })?;
            let server_js = app
                .path()
                .resolve("frontend/server.js", BaseDirectory::Resource)
                .map_err(|e| {
                    log_line(&log, &format!("FATAL frontend server.js missing: {e}"));
                    e
                })?;
            let frontend_dir = app
                .path()
                .resolve("frontend", BaseDirectory::Resource)
                .map_err(|e| {
                    log_line(&log, &format!("FATAL frontend resource missing: {e}"));
                    e
                })?;
            // Inventory the install tree: node.exe passes build-time
            // validation but is missing at runtime (AV quarantine? bad
            // extract?). One run answers whether bin/ is empty, partial,
            // or absent entirely.
            if let Some(res_root) = frontend_dir.parent() {
                log_dir(&log, "install root", res_root);
                log_dir(&log, "install root/bin", &res_root.join("bin"));
            }
            log_line(&log, "spawning node frontend...");
            log_line(&log, &format!("node = {} (exists={})", node.display(), node.is_file()));
            log_line(
                &log,
                &format!("server.js = {} (exists={})", server_js.display(), server_js.is_file()),
            );
            log_line(
                &log,
                &format!(
                    "frontend dir = {} (exists={})",
                    frontend_dir.display(),
                    frontend_dir.is_dir()
                ),
            );
            // Pass server.js by RELATIVE name with current_dir set: absolute
            // Windows paths with the verbatim \\?\ prefix break Node's
            // module resolution (EISDIR lstat 'C:'), while the exe path
            // itself launches fine.
            let (mut rx_web, _child_web) = app
                .shell()
                .command(node.to_string_lossy().as_ref())
                .args(["server.js", "-p", "3000"])
                .current_dir(&frontend_dir)
                .envs(env)
                .spawn()
                .map_err(|e| {
                    log_line(&log, &format!("FATAL failed to spawn frontend: {e}"));
                    e
                })?;
            log_line(&log, "frontend spawned");
            let log_web = log.clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx_web.recv().await {
                    match event {
                        CommandEvent::Stderr(line) => log_line(
                            &log_web,
                            &format!("[web] {}", String::from_utf8_lossy(&line)),
                        ),
                        CommandEvent::Stdout(line) => log_line(
                            &log_web,
                            &format!("[web] {}", String::from_utf8_lossy(&line)),
                        ),
                        CommandEvent::Terminated(payload) => log_line(
                            &log_web,
                            &format!("[web] process terminated: {payload:?}"),
                        ),
                        _ => {}
                    }
                }
            });
            log_line(&log, "setup complete");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run gaeclip shell");
}
