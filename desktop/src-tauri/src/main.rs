#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{atomic::{AtomicBool, Ordering}, Arc, Mutex};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::{process::{CommandChild, CommandEvent}, ShellExt};

struct Gateway(Mutex<Option<CommandChild>>);

fn main() {
    let smoke = std::env::args().skip(1).eq(["--gateway-self-test"]);
    // No arbitrary Python/shell arguments, IPC, trading runner or update command.
    let closing = Arc::new(AtomicBool::new(false));
    let on_close = closing.clone();
    let on_exit = closing.clone();
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(move |app| {
            let (mut events, child) = app.shell().sidecar("aitos-gateway")?
                .args([if smoke { "--gateway-self-test" } else { "--embedded" }]).spawn()?;
            app.manage(Gateway(Mutex::new(Some(child))));
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut opened = false;
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) if !smoke && !opened => {
                            let line = String::from_utf8_lossy(&bytes);
                            if let Some(origin) = line.trim().strip_prefix("AITOS_GATEWAY_ORIGIN=") {
                                let parsed = origin.parse::<tauri::Url>();
                                match parsed {
                                    Ok(url) if url.scheme() == "http" && url.host_str() == Some("127.0.0.1")
                                        && url.port().is_some_and(|p| p > 0) && url.username().is_empty()
                                        && url.password().is_none() && url.path() == "/"
                                        && url.query().is_none() && url.fragment().is_none() => {
                                        let allowed = url.origin();
                                        let window = WebviewWindowBuilder::new(&handle, "operator", WebviewUrl::External(url))
                                            .title("AITOS — Read-only Operator Console")
                                            .inner_size(1440.0, 900.0)
                                            .on_navigation(move |next| next.origin() == allowed)
                                            .build();
                                        if window.is_err() { handle.exit(2); }
                                        opened = true;
                                    }
                                    _ => handle.exit(2),
                                }
                            }
                        }
                        CommandEvent::Terminated(payload) => {
                            if !closing.load(Ordering::SeqCst) {
                                handle.exit(payload.code.unwrap_or(2));
                            }
                            break;
                        }
                        // Never forward child stderr/stdout to the webview or logs.
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                on_close.store(true, Ordering::SeqCst);
                window.app_handle().exit(0);
            }
        })
        .build(tauri::generate_context!())
        .expect("Desktop shell startup failed");
    application.run(move |app, event| {
        if let tauri::RunEvent::Exit = event {
            on_exit.store(true, Ordering::SeqCst);
            if let Some(child) = app.state::<Gateway>().0.lock().unwrap().take() {
                let _ = child.kill();
            }
        }
    });
}
