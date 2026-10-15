import CoreML
import Foundation
import WhisperKit

// wkspike transcribe <pcm> --model <name> [--prompt "<terms>"] [--block-s 60]
//                          [--offset-s N] [--limit-s N] --out <json>
//
// Reads int16 mono 16 kHz PCM, cuts it into fixed blocks (default 60 s, no overlap —
// the exact same rule the Python comparison script uses), runs WhisperKit per block
// and writes [{start, end, text}] with block/segment metadata and timings.

let sampleRate = 16000

struct Args {
    var pcm = ""
    var model = "openai_whisper-large-v3-v20240930_turbo"
    var prompt = ""
    var blockS = 60.0
    var offsetS = 0.0
    var limitS = 0.0 // 0 = to the end
    var out = ""
    var noSpeechThreshold: Float? = 0.6   // nil = не пропускать окна как тишину
    var tempFallback = 0                  // 0 = только temperature 0
    var carryContext = false              // хвост прошлого блока — в промпт следующего
    var modelsDir = NSString(string: "~/Claude/transcriber/spike/whisperkit/models").expandingTildeInPath
}

func parseArgs() -> Args {
    var a = Args()
    var rest = Array(CommandLine.arguments.dropFirst())
    guard rest.first == "transcribe" else {
        FileHandle.standardError.write("usage: wkspike transcribe <pcm> --model <name> [--prompt T] [--block-s 60] [--offset-s N] [--limit-s N] --out <json>\n".data(using: .utf8)!)
        exit(2)
    }
    rest.removeFirst()
    var positional: [String] = []
    var i = 0
    while i < rest.count {
        let t = rest[i]
        func next() -> String { i += 1; return i < rest.count ? rest[i] : "" }
        switch t {
        case "--model": a.model = next()
        case "--prompt": a.prompt = next()
        case "--block-s": a.blockS = Double(next()) ?? 60
        case "--offset-s": a.offsetS = Double(next()) ?? 0
        case "--limit-s": a.limitS = Double(next()) ?? 0
        case "--out": a.out = next()
        case "--models-dir": a.modelsDir = next()
        case "--no-speech-threshold": let v = next(); a.noSpeechThreshold = (v == "off") ? nil : Float(v)
        case "--temp-fallback": a.tempFallback = Int(next()) ?? 0
        case "--carry-context": a.carryContext = true
        default: positional.append(t)
        }
        i += 1
    }
    if let p = positional.first { a.pcm = p }
    guard !a.pcm.isEmpty, !a.out.isEmpty else {
        FileHandle.standardError.write("need <pcm> and --out\n".data(using: .utf8)!)
        exit(2)
    }
    return a
}

func readPCM(_ path: String, offsetS: Double, limitS: Double) throws -> [Float] {
    let data = try Data(contentsOf: URL(fileURLWithPath: path), options: .mappedIfSafe)
    let total = data.count / 2
    let from = min(Int(offsetS * Double(sampleRate)), total)
    let count = limitS > 0 ? min(Int(limitS * Double(sampleRate)), total - from) : total - from
    var out = [Float](repeating: 0, count: count)
    data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        let base = raw.baseAddress!
        for k in 0..<count {
            // PCM is little-endian int16; load unaligned to stay safe on the mapped buffer
            let v = base.load(fromByteOffset: (from + k) * 2, as: Int16.self)
            out[k] = Float(v) / 32768.0
        }
    }
    return out
}

struct OutSegment: Codable {
    let start: Double
    let end: Double
    let text: String
    let block: Int
    let avgLogprob: Float
    let noSpeechProb: Float
    let compressionRatio: Float
    let temperature: Float
}

struct OutBlock: Codable {
    let index: Int
    let start: Double
    let end: Double
    let wallSeconds: Double
    let segments: Int
}

struct Output: Codable {
    let model: String
    let modelFolder: String
    let prompt: String
    let promptTokens: Int
    let blockSeconds: Double
    let offsetSeconds: Double
    let audioSeconds: Double
    let wallSeconds: Double
    let loadSeconds: Double
    let realtimeFactor: Double
    let computeUnits: [String: String]
    let noSpeechThreshold: Float?
    let tempFallback: Int
    let blocks: [OutBlock]
    let segments: [OutSegment]
}

let args = parseArgs()
let samples = try readPCM(args.pcm, offsetS: args.offsetS, limitS: args.limitS)
let audioSeconds = Double(samples.count) / Double(sampleRate)
FileHandle.standardError.write("pcm: \(args.pcm) \(String(format: "%.1f", audioSeconds))s from \(args.offsetS)s\n".data(using: .utf8)!)

let loadStart = Date()
let config = WhisperKitConfig(
    model: args.model,
    downloadBase: URL(fileURLWithPath: args.modelsDir),
    modelRepo: "argmaxinc/whisperkit-coreml",
    verbose: true,
    logLevel: .info,
    prewarm: false,
    load: true,
    download: true
)
let whisperKit = try await WhisperKit(config)
let loadSeconds = Date().timeIntervalSince(loadStart)
let modelFolder = whisperKit.modelFolder?.path ?? "(unknown)"
FileHandle.standardError.write("model folder: \(modelFolder)  load \(String(format: "%.1f", loadSeconds))s\n".data(using: .utf8)!)

let compute = whisperKit.modelCompute
func cuName(_ u: MLComputeUnits) -> String {
    switch u {
    case .cpuOnly: return "cpuOnly"
    case .cpuAndGPU: return "cpuAndGPU"
    case .all: return "all"
    case .cpuAndNeuralEngine: return "cpuAndNeuralEngine"
    @unknown default: return "unknown"
    }
}
let computeUnits = [
    "mel": cuName(compute.melCompute),
    "audioEncoder": cuName(compute.audioEncoderCompute),
    "textDecoder": cuName(compute.textDecoderCompute),
]
FileHandle.standardError.write("compute: \(computeUnits)\n".data(using: .utf8)!)

// Prompt → tokens, exactly as WhisperKit's own CLI does it.
guard let tokenizer = whisperKit.tokenizer else {
    FileHandle.standardError.write("no tokenizer\n".data(using: .utf8)!)
    exit(1)
}
let carryMax = 400   // символов хвоста, как CARRY_MAX в воркере
func encodePrompt(_ text: String) -> [Int]? {
    let t = text.trimmingCharacters(in: .whitespaces)
    if t.isEmpty { return nil }
    return tokenizer.encode(text: " " + t).filter { $0 < tokenizer.specialTokens.specialTokenBegin }
}
let basePromptTokens = encodePrompt(args.prompt)
FileHandle.standardError.write("prompt tokens: \(basePromptTokens?.count ?? 0)\n".data(using: .utf8)!)

let options = DecodingOptions(
    task: .transcribe,
    language: "ru",
    temperature: 0.0,
    temperatureFallbackCount: args.tempFallback,
    usePrefillPrompt: true,
    detectLanguage: false,
    skipSpecialTokens: true,
    withoutTimestamps: false,
    promptTokens: basePromptTokens,
    noSpeechThreshold: args.noSpeechThreshold,
    chunkingStrategy: nil
)

let blockSamples = Int(args.blockS * Double(sampleRate))
var outSegments: [OutSegment] = []
var outBlocks: [OutBlock] = []
let runStart = Date()

var blockIndex = 0
var pos = 0
var carry = ""
while pos < samples.count {
    let end = min(pos + blockSamples, samples.count)
    let chunk = Array(samples[pos..<end])
    let blockStart = Double(pos) / Double(sampleRate) + args.offsetS
    let blockEnd = Double(end) / Double(sampleRate) + args.offsetS
    var blockOptions = options
    if args.carryContext, !carry.isEmpty {
        let tail = String(carry.suffix(carryMax))
        blockOptions.promptTokens = encodePrompt("\(args.prompt) \(tail)")
    }
    let t0 = Date()
    let results = try await whisperKit.transcribe(audioArray: chunk, decodeOptions: blockOptions)
    let wall = Date().timeIntervalSince(t0)
    var n = 0
    for r in results {
        for s in r.segments {
            let text = s.text.trimmingCharacters(in: .whitespaces)
            if text.isEmpty { continue }
            outSegments.append(OutSegment(
                start: blockStart + Double(s.start),
                end: blockStart + Double(s.end),
                text: text,
                block: blockIndex,
                avgLogprob: s.avgLogprob,
                noSpeechProb: s.noSpeechProb,
                compressionRatio: s.compressionRatio,
                temperature: s.temperature
            ))
            n += 1
        }
    }
    carry = results.flatMap { $0.segments }.map { $0.text }.joined(separator: " ")
        .trimmingCharacters(in: .whitespaces)
    outBlocks.append(OutBlock(index: blockIndex, start: blockStart, end: blockEnd, wallSeconds: wall, segments: n))
    FileHandle.standardError.write("block \(blockIndex) \(String(format: "%.0f-%.0f", blockStart, blockEnd))s -> \(n) seg in \(String(format: "%.1f", wall))s\n".data(using: .utf8)!)
    blockIndex += 1
    pos = end
}

let wallSeconds = Date().timeIntervalSince(runStart)
let out = Output(
    model: args.model,
    modelFolder: modelFolder,
    prompt: args.prompt,
    promptTokens: basePromptTokens?.count ?? 0,
    blockSeconds: args.blockS,
    offsetSeconds: args.offsetS,
    audioSeconds: audioSeconds,
    wallSeconds: wallSeconds,
    loadSeconds: loadSeconds,
    realtimeFactor: wallSeconds > 0 ? audioSeconds / wallSeconds : 0,
    computeUnits: computeUnits,
    noSpeechThreshold: args.noSpeechThreshold,
    tempFallback: args.tempFallback,
    blocks: outBlocks,
    segments: outSegments
)
let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
try encoder.encode(out).write(to: URL(fileURLWithPath: args.out))
FileHandle.standardError.write("wrote \(args.out): \(outSegments.count) segments, \(String(format: "%.1f", wallSeconds))s wall, x\(String(format: "%.2f", out.realtimeFactor)) realtime\n".data(using: .utf8)!)
