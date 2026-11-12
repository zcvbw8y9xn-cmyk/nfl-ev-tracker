import AppKit
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKUIDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var serverProcess: Process?
    private var stdoutPipe: Pipe?
    private var stderrBuffer = Data()
    private var hasLoadedURL = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        buildWindow()
        startBackendServer()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopBackendServer()
    }

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About NFL EV Tracker", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide NFL EV Tracker", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit NFL EV Tracker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        let viewMenuItem = NSMenuItem()
        mainMenu.addItem(viewMenuItem)
        let viewMenu = NSMenu(title: "View")
        let reloadItem = NSMenuItem(title: "Reload Window", action: #selector(reloadWebView), keyEquivalent: "r")
        viewMenu.addItem(reloadItem)
        viewMenuItem.submenu = viewMenu

        let winMenuItem = NSMenuItem()
        mainMenu.addItem(winMenuItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Zoom / Maximize", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winMenuItem.submenu = winMenu

        NSApp.mainMenu = mainMenu
    }

    private func buildWindow() {
        let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let frame = screenFrame
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.delegate = self
        window.title = "NFL +EV & Pick'em Tracker"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.043, green: 0.055, blue: 0.078, alpha: 1.0)
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 680, height: 500)
        window.collectionBehavior = [.fullScreenPrimary]
        window.setFrame(screenFrame, display: true)

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: frame, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.setValue(false, forKey: "drawsBackground")

        let container = NSView(frame: frame)
        container.autoresizingMask = [.width, .height]
        container.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            webView.topAnchor.constraint(equalTo: container.topAnchor),
            webView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])

        let splash = """
        <html><body style="margin:0;background:#0b0e14;color:#eef2f8;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;">
          <div style="text-align:center;">
            <div style="font-size:20px;font-weight:700;margin-bottom:8px;">NFL +EV &amp; Pick'em Tracker</div>
            <div style="font-size:13px;color:#9aa7bd;">Starting local engine…</div>
          </div>
        </body></html>
        """
        webView.loadHTMLString(splash, baseURL: nil)

        window.contentView = container
        window.setFrame(screenFrame, display: true)
        window.makeKeyAndOrderFront(nil)
    }

    func windowDidResize(_ notification: Notification) {
        webView?.needsLayout = true
    }

    func windowDidChangeScreen(_ notification: Notification) {
        webView?.needsLayout = true
    }

    @objc private func reloadWebView() {
        webView.reload()
    }

    private func resolveConfig() -> (projectRoot: String, pythonPath: String) {
        let info = Bundle.main.infoDictionary
        let root = (info?["NFLEVProjectRoot"] as? String)
            ?? FileManager.default.homeDirectoryForCurrentUser.path + "/.gemini/antigravity/scratch/nfl-ev-tracker"
        let py = (info?["NFLEVPythonPath"] as? String) ?? "/opt/homebrew/bin/python3"
        return (root, py)
    }

    private func startBackendServer() {
        let cfg = resolveConfig()
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: cfg.pythonPath)
        proc.currentDirectoryURL = URL(fileURLWithPath: cfg.projectRoot)
        proc.arguments = ["-u", "-m", "src.app_server", "--port", "0"]

        var env = ProcessInfo.processInfo.environment
        let extraPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        env["PATH"] = extraPath + ":" + (env["PATH"] ?? "")
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe
        self.stdoutPipe = outPipe

        var stdoutAcc = ""
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let self = self else { return }
            if let chunk = String(data: data, encoding: .utf8) {
                stdoutAcc += chunk
                for line in stdoutAcc.components(separatedBy: .newlines) {
                    if line.hasPrefix("READY "), !self.hasLoadedURL {
                        let urlStr = line.replacingOccurrences(of: "READY ", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
                        if let url = URL(string: urlStr) {
                            self.hasLoadedURL = true
                            DispatchQueue.main.async {
                                self.webView.load(URLRequest(url: url))
                            }
                        }
                    }
                }
            }
        }

        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if !data.isEmpty {
                self?.stderrBuffer.append(data)
            }
        }

        proc.terminationHandler = { [weak self] p in
            guard let self = self else { return }
            if !self.hasLoadedURL && p.terminationStatus != 0 {
                let errText = String(data: self.stderrBuffer, encoding: .utf8) ?? "Backend failed to start."
                DispatchQueue.main.async {
                    let escErr = errText
                        .replacingOccurrences(of: "&", with: "&amp;")
                        .replacingOccurrences(of: "<", with: "&lt;")
                    let html = """
                    <html><body style="margin:0;background:#0b0e14;color:#fca5a5;font-family:-apple-system,sans-serif;padding:32px;">
                      <h2>Backend Startup Error</h2>
                      <pre style="white-space:pre-wrap;background:#121722;padding:16px;border-radius:8px;color:#eef2f8;">\(escErr)</pre>
                    </body></html>
                    """
                    self.webView.loadHTMLString(html, baseURL: nil)
                }
            }
        }

        do {
            try proc.run()
            self.serverProcess = proc
        } catch {
            let alert = NSAlert()
            alert.messageText = "Failed to launch Python backend"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    private func stopBackendServer() {
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
        }
        serverProcess = nil
    }

    // Native macOS JS alert / confirm support
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "NFL +EV Tracker"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "NFL +EV Tracker"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
