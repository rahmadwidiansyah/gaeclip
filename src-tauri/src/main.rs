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
use tauri::path::BaseDirectory;
use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

fn sidecar_env(app: &tauri::AppHandle) -> HashMap<String, String> {
    let root = app
        .path()
        .app_local_data_dir()
        .unwrap_or_else(|_| std::path::PathBuf::from("."))
        .join("Gaeclip")
        .join("storage");
    let _ = std::fs::create_dir_all(&root);
    let mut env = HashMap::new();
    env.insert("GAECLIP_RUNTIME".to_string(), "desktop".to_string());
    env.insert(
        "STORAGE_ROOT".to_string(),
        root.to_string_lossy().into_owned(),
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
            let env = sidecar_env(app.handle());

            // 1. Backend API (LocalRunner inside; no Redis/Celery/Docker).
            let (mut rx_api, _child_api) = app
                .shell()
                .sidecar("gaeclip-api")
                .expect("api sidecar missing")
                .envs(env.clone())
                .spawn()
                .expect("failed to spawn api sidecar");
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx_api.recv().await {
                    if let CommandEvent::Stderr(line) = event {
                        eprintln!("[api] {}", String::from_utf8_lossy(&line));
                    }
                }
            });

            // 2. Frontend: Next.js standalone served by bundled node.
            let node = app
                .path()
                .resolve("bin/node.exe", BaseDirectory::Resource)
                .expect("node sidecar resource missing");
            let server_js = app
                .path()
                .resolve("frontend/server.js", BaseDirectory::Resource)
                .expect("frontend server.js missing");
            let frontend_dir = app
                .path()
                .resolve("frontend", BaseDirectory::Resource)
                .expect("frontend resource missing");
            let (mut rx_web, _child_web) = app
                .shell()
                .command(node.to_string_lossy().as_ref())
                .args([server_js.to_string_lossy().as_ref(), "-p", "3000"])
                .current_dir(frontend_dir)
                .envs(env)
                .spawn()
                .expect("failed to spawn frontend");
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx_web.recv().await {
                    if let CommandEvent::Stderr(line) = event {
                        eprintln!("[web] {}", String::from_utf8_lossy(&line));
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run gaeclip shell");
}
