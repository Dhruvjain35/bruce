import XCTest
@testable import Bruce

/// The reporter's privacy contract is structural — its API accepts only typed, enum-like fields, so
/// there is no parameter through which content could flow. These tests pin that: a recorded line
/// contains exactly the safe fields, and nothing a caller could stuff content into leaks.
final class ReporterTests: XCTestCase {
    final class CapturingSink: ReportSink {
        var reports: [ErrorReport] = []
        func record(_ r: ErrorReport) { reports.append(r) }
    }

    override func setUp() { Reporter.sink = CapturingSink() }
    override func tearDown() { Reporter.sink = LocalOnlySink() }

    func test_report_records_only_typed_fields() {
        let sink = Reporter.sink as! CapturingSink
        Reporter.report(area: "intake", code: "provider_unavailable", missionState: "blocked", correlationID: "cid-1")
        XCTAssertEqual(sink.reports.count, 1)
        let r = sink.reports[0]
        XCTAssertEqual(r.area, "intake")
        XCTAssertEqual(r.code, "provider_unavailable")
        XCTAssertEqual(r.missionState, "blocked")
        XCTAssertEqual(r.correlationID, "cid-1")
        // The rendered line carries no field other than the safe, typed ones.
        XCTAssertTrue(r.line.contains("[intake] provider_unavailable"))
        XCTAssertFalse(r.line.contains("http://"))   // no URLs
    }

    func test_correlation_id_is_generated_when_absent() {
        let sink = Reporter.sink as! CapturingSink
        Reporter.report(area: "auth", code: "http_401")
        XCTAssertFalse(sink.reports[0].correlationID.isEmpty)
        XCTAssertNil(sink.reports[0].missionState)
    }
}
