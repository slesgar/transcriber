import Foundation
import Testing
@testable import TranscriberCore

@Test func nodeLineSkipsEmptyAttributesAndIndentsByDepth() {
    var node = AXNodeRecord()
    node.role = "AXGroup"
    node.domClass = "participant speaking"
    node.selected = true
    #expect(node.line(depth: 2) == "    AXGroup class='participant speaking' selected=true")
}

@Test func nodeLineKeepsFilledAttributesInOrderWithFrame() {
    var node = AXNodeRecord()
    node.role = "AXStaticText"
    node.subrole = "AXContentList"
    node.title = "Иван Сидоров"
    node.desc = "говорит"
    node.value = "Иван Сидоров"
    node.frame = CGRect(x: 10, y: 20.4, width: 100, height: 30)
    #expect(node.line(depth: 0) ==
        "AXStaticText subrole='AXContentList' title='Иван Сидоров' desc='говорит' value='Иван Сидоров' frame=10,20 100x30")
}

@Test func longValuesAreCutAndNewlinesFlattened() {
    let long = String(repeating: "я", count: 120)
    #expect(AXNodeRecord.cut(long).count == AXNodeRecord.valueLimit + 1)   // +1 for the ellipsis
    #expect(AXNodeRecord.cut("две\nстроки") == "две⏎строки")
}
