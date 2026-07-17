import Foundation
import os
import UIKit

/// Content-free crash & error reporting.
///
/// PRIVACY CONTRACT (enforced by the typed API below — there is no field that can carry content):
///   RECORDED   : app version/build · device+OS category · feature area · typed error code ·
///                correlation id · mission state (only the small enum-y set that is safe)
///   NEVER       : flyer text, screenshots, PDFs, extracted content, email addresses, tokens,
///                Apple identity data, student-written text, URLs, or provider error bodies.
///
/// Local-only until a provider is explicitly chosen: events go to the unified log (`os.Logger`,
/// private redaction) and an in-memory ring buffer — NOTHING leaves the device, and NO third-party
/// SDK is linked. Swapping in a provider later is one conformance to `ReportSink`.
struct ErrorReport {
    let area: String          // feature area, e.g. "intake", "auth", "home", "account_delete"
    let code: String          // typed error code, e.g. "provider_unavailable", "http_401", "crash"
    let missionState: String? // only a known phase string, never content
    let correlationID: String
    let appVersion: String
    let deviceCategory: String

    var line: String { "[\(area)] \(code) mission=\(missionState ?? "-") cid=\(correlationID) v=\(appVersion) dev=\(deviceCategory)" }
}

protocol ReportSink { func record(_ report: ErrorReport) }

/// Default sink: local only. Logs (privacy .public is safe — every field is content-free) + keeps a
/// small ring buffer for in-app diagnostics. No network.
final class LocalOnlySink: ReportSink {
    private let log = Logger(subsystem: "com.brucedev.Bruce", category: "report")
    private(set) var recent: [String] = []
    func record(_ r: ErrorReport) {
        log.error("\(r.line, privacy: .public)")
        recent.append(r.line)
        if recent.count > 100 { recent.removeFirst(recent.count - 100) }
    }
}

enum Reporter {
    /// Replace with a provider-backed sink ONLY after one is explicitly chosen (see docs/alpha-readiness).
    static var sink: ReportSink = LocalOnlySink()

    static func newCorrelationID() -> String { UUID().uuidString }

    private static var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        return "\(v)(\(b))"
    }
    private static var deviceCategory: String {
        // Category only — model family + major OS. Never a unique device identifier.
        "\(UIDevice.current.userInterfaceIdiom == .pad ? "ipad" : "iphone")/iOS\(ProcessInfo.processInfo.operatingSystemVersion.majorVersion)"
    }

    /// Report a typed, content-free error. `code`/`area` must be enum-like constants, never a message.
    static func report(area: String, code: String, missionState: String? = nil, correlationID: String? = nil) {
        sink.record(ErrorReport(area: area, code: code, missionState: missionState,
                                correlationID: correlationID ?? newCorrelationID(),
                                appVersion: appVersion, deviceCategory: deviceCategory))
    }

    /// Install a content-free uncaught-exception handler: records the exception's TYPE name only
    /// (never its reason, which could in principle carry data) plus a crash marker.
    static func installCrashHandler() {
        NSSetUncaughtExceptionHandler { exception in
            Reporter.report(area: "crash", code: "uncaught_\(exception.name.rawValue)")
        }
    }
}
