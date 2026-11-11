#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use zeroize::{Zeroize, Zeroizing};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

struct Gateway(Mutex<Option<CommandChild>>);

struct AdminChild(Option<CommandChild>);

impl Drop for AdminChild {
    fn drop(&mut self) {
        if let Some(child) = self.0.take() {
            let _ = child.kill();
        }
    }
}

fn is_local_signer_url(url: &tauri::Url) -> bool {
    let host = url.host_str();
    let production_origin = (url.scheme() == "tauri" && host == Some("localhost"))
        || (url.scheme() == "http" && host == Some("tauri.localhost"));
    let production_origin = production_origin && url.port().is_none();
    let development_origin = cfg!(debug_assertions)
        && url.scheme() == "http"
        && matches!(host, Some("localhost") | Some("127.0.0.1"))
        && url.port() == Some(1420);
    (production_origin || development_origin)
        && url.path() == "/signer.html"
        && url.query().is_none()
        && url.fragment().is_none()
        && url.username().is_empty()
        && url.password().is_none()
}

#[tauri::command]
async fn signer_credential_admin(
    window: WebviewWindow,
    app: AppHandle,
    request: String,
) -> Result<String, String> {
    let request = Zeroizing::new(request);
    if window.label() != "signer" {
        return Err("signer_window_required".into());
    }
    let url = window.url().map_err(|_| "signer_origin_unavailable")?;
    if !is_local_signer_url(&url) {
        return Err("trusted_local_signer_origin_required".into());
    }
    if request.is_empty() || request.len() > 8192 || request.contains('\0') {
        return Err("signer_admin_request_size_invalid".into());
    }

    let (mut events, child) = app
        .shell()
        .sidecar("aitos-gateway")
        .map_err(|_| "signer_admin_sidecar_unavailable")?
        .args(["--credential-admin"])
        .spawn()
        .map_err(|_| "signer_admin_sidecar_unavailable")?;
    let mut child = AdminChild(Some(child));
    let mut input = request.as_bytes().to_vec();
    input.push(b'\n');
    let write_failed = child
        .0
        .as_mut()
        .is_none_or(|process| process.write(&input).is_err());
    input.zeroize();
    if write_failed {
        return Err("signer_admin_request_delivery_failed".into());
    }

    let result = tokio::time::timeout(Duration::from_secs(20), async {
        let mut output = Vec::new();
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    if output.len() + bytes.len() > 8192 {
                        return Err("signer_admin_response_size_invalid");
                    }
                    output.extend_from_slice(&bytes);
                }
                CommandEvent::Terminated(_) => {
                    if output.is_empty() || !output.ends_with(b"\n") {
                        return Err("signer_admin_response_invalid");
                    }
                    let response = String::from_utf8(output)
                        .map_err(|_| "signer_admin_response_encoding_invalid")?;
                    if !response.starts_with("{\"ok\":") {
                        return Err("signer_admin_response_invalid");
                    }
                    return Ok(response);
                }
                _ => {}
            }
        }
        Err("signer_admin_sidecar_terminated")
    })
    .await;

    if result.is_err() {
        return Err("signer_admin_operation_timeout".into());
    }
    result
        .map_err(|_| "signer_admin_operation_timeout")?
        .map_err(str::to_owned)
}

fn main() {
    let smoke = std::env::args().skip(1).eq(["--gateway-self-test"]);
    // The operator window is an external read-only origin. Only the bundled
    // signer window receives the signer-local capability and native command.
    let closing = Arc::new(AtomicBool::new(false));
    let on_close = closing.clone();
    let on_exit = closing.clone();
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![signer_credential_admin])
        .setup(move |app| {
            let handle = app.handle().clone();
            let signer = WebviewWindowBuilder::new(
                &handle,
                "signer",
                WebviewUrl::App("signer.html".into()),
            )
            .title("AITOS — Trusted Signer Settings")
            .inner_size(920.0, 900.0)
            .min_inner_size(360.0, 620.0)
            .on_navigation(is_local_signer_url)
            .build()?;
            let _ = signer;

            let (mut events, child) = app
                .shell()
                .sidecar("aitos-gateway")?
                .args([if smoke {
                    "--gateway-self-test"
                } else {
                    "--embedded"
                }])
                .spawn()?;
            app.manage(Gateway(Mutex::new(Some(child))));
            tauri::async_runtime::spawn(async move {
                let mut opened = false;
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) if !smoke && !opened => {
                            let line = String::from_utf8_lossy(&bytes);
                            if let Some(origin) =
                                line.trim().strip_prefix("AITOS_GATEWAY_ORIGIN=")
                            {
                                let parsed = origin.parse::<tauri::Url>();
                                match parsed {
                                    Ok(url)
                                        if url.scheme() == "http"
                                            && url.host_str() == Some("127.0.0.1")
                                            && url.port().is_some_and(|p| p > 0)
                                            && url.username().is_empty()
                                            && url.password().is_none()
                                            && url.path() == "/"
                                            && url.query().is_none()
                                            && url.fragment().is_none() =>
                                    {
                                        let allowed = url.origin();
                                        let window = WebviewWindowBuilder::new(
                                            &handle,
                                            "operator",
                                            WebviewUrl::External(url),
                                        )
                                        .title("AITOS — Read-only Operator Console")
                                        .inner_size(1440.0, 900.0)
                                        .on_navigation(move |next| next.origin() == allowed)
                                        .build();
                                        if window.is_err() {
                                            handle.exit(2);
                                        }
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
