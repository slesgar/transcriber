import Foundation

/// The owner's own agent — Claude, Codex, a local model, anything with a command line that reads
/// the question on stdin and prints the answer on stdout. Lives in `app-config.json` under its own
/// `agent` key. An empty command means «no agent»: the app never guesses one.
public struct AgentConfig: Codable, Equatable {
    public var command: String
    /// Folder the agent starts in when the owner asks «with project context»; empty — none.
    public var contextDir: String
    public var timeoutS: Int

    public init(command: String, contextDir: String, timeoutS: Int) {
        self.command = command
        self.contextDir = contextDir
        self.timeoutS = timeoutS
    }

    public static let defaults = AgentConfig(command: "", contextDir: "", timeoutS: 180)

    public var isConfigured: Bool { !command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    enum CodingKeys: String, CodingKey {
        case command
        case contextDir = "context_dir"
        case timeoutS = "timeout_s"
    }

    /// Every key is optional: a half-written block means «this one thing», not «throw it away».
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AgentConfig.defaults
        command = try c.decodeIfPresent(String.self, forKey: .command) ?? d.command
        contextDir = try c.decodeIfPresent(String.self, forKey: .contextDir) ?? d.contextDir
        timeoutS = try c.decodeIfPresent(Int.self, forKey: .timeoutS) ?? d.timeoutS
    }

    /// The `agent` key of the JSON at `url`; missing file, key or a broken block — the defaults.
    public static func load(from url: URL = AppPaths.appConfig) -> AgentConfig {
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let agent = object["agent"] else { return defaults }
        guard let sub = try? JSONSerialization.data(withJSONObject: agent, options: [.fragmentsAllowed]),
              let config = try? JSONDecoder().decode(AgentConfig.self, from: sub) else {
            Log.shared.warn("agent config: 'agent' in \(url.path) is malformed, using defaults")
            return defaults
        }
        return config
    }

    static let comment = "свой агент для вопросов к идущей встрече: command — команда оболочки, "
        + "читает вопрос со stdin и пишет ответ в stdout (пусто — агент не настроен); context_dir — "
        + "папка, из которой агент запускается, когда включён контекст проектов; timeout_s — сколько ждать ответа."
}

public enum AgentResult: Equatable {
    case answer(String)
    case failed(code: Int32, message: String)
    case timedOut
    case notConfigured
}

/// Runs the agent command through the owner's shell. The app is started by launchd with a bare
/// PATH, so the PATH of an interactive shell is asked once (`ShellPath`) and handed to a login
/// shell that runs the command — the same `claude` or `codex` the owner types in Terminal.
public enum AgentRunner {
    public static func run(command: String, prompt: String, cwd: URL?, timeout: TimeInterval,
                           queue: DispatchQueue = .main, completion: @escaping (AgentResult) -> Void) {
        let command = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !command.isEmpty else {
            queue.async { completion(.notConfigured) }
            return
        }
        let once = Once(queue: queue, completion: completion)
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: ShellPath.shell)
            process.arguments = ["-lc", command]
            var env = ProcessInfo.processInfo.environment
            if let path = ShellPath.discovered { env["PATH"] = path }
            process.environment = env
            if let cwd { process.currentDirectoryURL = cwd }
            let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
            process.standardInput = stdin
            process.standardOutput = stdout
            process.standardError = stderr
            do {
                try process.run()
            } catch {
                once.finish(.failed(code: -1, message: "не запустился: \(error.localizedDescription)"))
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                guard process.isRunning else { return }
                process.terminate()
                once.finish(.timedOut)
            }
            // stdin is written on its own thread: a large prompt must not block the readers below.
            DispatchQueue.global().async {
                stdin.fileHandleForWriting.write(Data(prompt.utf8))
                try? stdin.fileHandleForWriting.close()
            }
            var errData = Data()
            let errDone = DispatchSemaphore(value: 0)
            DispatchQueue.global().async {
                errData = stderr.fileHandleForReading.readDataToEndOfFile()
                errDone.signal()
            }
            let outData = stdout.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            errDone.wait()
            let out = String(decoding: outData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            let err = String(decoding: errData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            let code = process.terminationStatus
            if code != 0 {
                once.finish(.failed(code: code, message: String(err.suffix(400))))
            } else if out.isEmpty {
                once.finish(.failed(code: 0, message: "агент ничего не ответил"))
            } else {
                once.finish(.answer(out))
            }
        }
    }

    /// The completion runs exactly once: a timeout and the process exit race each other.
    private final class Once {
        private let lock = NSLock()
        private var done = false
        private let queue: DispatchQueue
        private let completion: (AgentResult) -> Void

        init(queue: DispatchQueue, completion: @escaping (AgentResult) -> Void) {
            self.queue = queue
            self.completion = completion
        }

        func finish(_ result: AgentResult) {
            lock.lock()
            defer { lock.unlock() }
            guard !done else { return }
            done = true
            queue.async { self.completion(result) }
        }
    }
}

/// The owner's shell and the PATH an interactive session of it sets up (conda, Homebrew, npm —
/// whatever `.zshrc` adds). Asked once, lazily; a shell that fails to answer leaves the
/// inherited PATH in place.
public enum ShellPath {
    public static var shell: String {
        let value = ProcessInfo.processInfo.environment["SHELL"] ?? ""
        return value.isEmpty ? "/bin/zsh" : value
    }

    static let marker = "__TRANSCRIBER_PATH__"

    public static let discovered: String? = {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: shell)
        p.arguments = ["-ilc", "printf '\\n\(marker)%s' \"$PATH\""]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        p.waitUntilExit()
        return parse(text)
    }()

    /// Rc files may print anything before the marker; only what follows it is the PATH.
    static func parse(_ text: String) -> String? {
        guard let range = text.range(of: marker, options: .backwards) else { return nil }
        let path = text[range.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
    }
}
