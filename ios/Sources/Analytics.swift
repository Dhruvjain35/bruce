import Foundation
import os

/// Content-free product analytics for the capture → mission journey.
///
/// STRICT RULE: events carry only enum-typed facts (which source type, which stage, a short failure
/// REASON code) — never document contents, pasted text, file names, URLs, or any student-private
/// data. If a value could contain what the student handed Bruce, it does not go here.
enum IntakeEvent: Equatable {
    case sheetOpened
    case sourceSelected(SourceType)
    case submissionStarted(SourceType)
    case missionAcknowledged        // 202 received; durable mission exists
    case extractionCompleted        // server reached awaiting_approval
    case extractionFailed(Reason)   // terminal failure
    case userAbandoned(Stage)       // dismissed before the mission was acknowledged/ready
    case retryUsed

    enum SourceType: String { case photo, screenshot, pdf, text, link }
    enum Reason: String { case unreadable, providerUnavailable, sessionExpired, network, timeout }
    enum Stage: String { case picking, entering, submitting, working }

    var name: String {
        switch self {
        case .sheetOpened: return "intake.sheet_opened"
        case .sourceSelected: return "intake.source_selected"
        case .submissionStarted: return "intake.submission_started"
        case .missionAcknowledged: return "intake.mission_acknowledged"
        case .extractionCompleted: return "intake.extraction_completed"
        case .extractionFailed: return "intake.extraction_failed"
        case .userAbandoned: return "intake.user_abandoned"
        case .retryUsed: return "intake.retry_used"
        }
    }

    /// Only enum raw values — provably free of student content.
    var attribute: String? {
        switch self {
        case .sourceSelected(let t), .submissionStarted(let t): return t.rawValue
        case .extractionFailed(let r): return r.rawValue
        case .userAbandoned(let s): return s.rawValue
        default: return nil
        }
    }
}

enum Analytics {
    private static let log = Logger(subsystem: "com.brucedev.Bruce", category: "intake")
    /// Test seam: capture events in-process without a network sink.
    static var sink: ((IntakeEvent) -> Void)? = nil

    static func track(_ event: IntakeEvent) {
        sink?(event)
        if let attr = event.attribute {
            log.info("\(event.name, privacy: .public) [\(attr, privacy: .public)]")
        } else {
            log.info("\(event.name, privacy: .public)")
        }
    }
}
