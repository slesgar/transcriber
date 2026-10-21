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

    /// Every key is optional and read on its own: a half-written or mistyped block costs only the
    /// key that is wrong, never the command next to it.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AgentConfig.defaults
        command = (try? c.decodeIfPresent(String.self, forKey: .command)) ?? d.command
        contextDir = (try? c.decodeIfPresent(String.self, forKey: .contextDir)) ?? d.contextDir
        timeoutS = (try? c.decodeIfPresent(Int.self, forKey: .timeoutS)) ?? d.timeoutS
    }

    /// The context folder with `~` expanded; nil when none is set.
    public var contextURL: URL? {
        let p = contextDir.trimmingCharacters(in: .whitespaces)
        return p.isEmpty ? nil : URL(fileURLWithPath: FolderStatus.resolve(p, inside: nil), isDirectory: true)
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
    case cancelled
}

/// A running question: `cancel()` stops the agent and everything it started.
public final class AgentRun {
    fileprivate let once: AgentRunner.Once
    fileprivate var pid: pid_t = 0
    private let lock = NSLock()

    fileprivate init(once: AgentRunner.Once) { self.once = once }

    fileprivate func started(_ pid: pid_t) {
        lock.lock(); self.pid = pid; lock.unlock()
    }

    public func cancel() {
        lock.lock(); let pid = self.pid; lock.unlock()
        once.finish(.cancelled)
        if pid > 0 { ProcessTree.terminate(pid) }
    }
}

/// Runs the agent command through the owner's shell. The app is started by launchd with a bare
/// PATH, so the PATH of an interactive shell is asked once (`ShellPath`) and handed to a login
/// shell that runs the command — the same `claude` or `codex` the owner types in Terminal.
public enum AgentRunner {
    @discardableResult
    public static func run(command: String, prompt: String, cwd: URL?, timeout: TimeInterval,
                           queue: DispatchQueue = .main, completion: @escaping (AgentResult) -> Void) -> AgentRun {
        let command = command.trimmingCharacters(in: .whitespacesAndNewlines)
        let once = Once(queue: queue, completion: completion)
        let handle = AgentRun(once: once)
        guard !command.isEmpty else {
            once.finish(.notConfigured)
            return handle
        }
        DispatchQueue.global(qos: .userInitiated).async {
            if once.isDone { return }   // cancelled before it started
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
            let pid = process.processIdentifier
            handle.started(pid)
            if once.isDone { ProcessTree.terminate(pid) }   // cancelled while it was starting
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                guard process.isRunning else { return }
                once.finish(.timedOut)
                ProcessTree.terminate(pid)
            }
            // stdin is written on its own thread: a large prompt must not block the readers below.
            // An agent that exits without reading it must cost an EPIPE, not a SIGPIPE that would
            // take the whole app — and the recording — down.
            let writer = stdin.fileHandleForWriting
            _ = fcntl(writer.fileDescriptor, F_SETNOSIGPIPE, 1)
            DispatchQueue.global().async {
                try? writer.write(contentsOf: Data(prompt.utf8))
                try? writer.close()
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
        return handle
    }

    /// The completion runs exactly once: a timeout, a cancel and the process exit race each other.
    final class Once {
        private let lock = NSLock()
        private var done = false
        private let queue: DispatchQueue
        private let completion: (AgentResult) -> Void

        init(queue: DispatchQueue, completion: @escaping (AgentResult) -> Void) {
            self.queue = queue
            self.completion = completion
        }

        var isDone: Bool {
            lock.lock(); defer { lock.unlock() }
            return done
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
    /// `$SHELL`, else the login shell of the account (launchd may not pass `$SHELL`), else zsh.
    public static var shell: String {
        if let value = ProcessInfo.processInfo.environment["SHELL"], !value.isEmpty { return value }
        if let entry = getpwuid(getuid()), let sh = entry.pointee.pw_shell {
            let value = String(cString: sh)
            if !value.isEmpty { return value }
        }
        return "/bin/zsh"
    }

    /// A heavy rc file (conda, plugin managers) takes seconds; one that hangs must not hang us.
    static let deadline: TimeInterval = 20

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
        let pid = p.processIdentifier
        let timer = DispatchWorkItem { if p.isRunning { ProcessTree.terminate(pid) } }
        DispatchQueue.global().asyncAfter(deadline: .now() + deadline, execute: timer)
        let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        p.waitUntilExit()
        timer.cancel()
        let path = parse(text)
        if path == nil { Log.shared.warn("shell path: \(shell) gave no PATH within \(Int(deadline)) s, using the inherited one") }
        return path
    }()

    /// Rc files may print anything before the marker; only what follows it is the PATH.
    static func parse(_ text: String) -> String? {
        guard let range = text.range(of: marker, options: .backwards) else { return nil }
        let path = text[range.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
    }
}

/// Stops a command and everything it started: `cd x && agent`, `agent | tee` or a wrapper script
/// leave grandchildren holding the output pipe, so killing the shell alone is not enough.
/// SIGTERM to the whole tree, SIGKILL to whatever is still there two seconds later.
enum ProcessTree {
    static func terminate(_ root: pid_t) {
        let pids = [root] + descendants(of: root)
        for pid in pids { kill(pid, SIGTERM) }
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
            for pid in pids where kill(pid, 0) == 0 { kill(pid, SIGKILL) }
        }
    }

    /// Children by `pgrep -P`, depth first.
    static func descendants(of pid: pid_t) -> [pid_t] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        p.arguments = ["-P", String(pid)]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return [] }
        let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        p.waitUntilExit()
        let children = text.split(whereSeparator: \.isNewline).compactMap { pid_t($0) }
        return children.flatMap { [$0] + descendants(of: $0) }
    }
}

/// Agents the app knows how to call in one-shot mode, found on the owner's PATH. The commands are
/// the scheme, not an installation value: each runs the agent once, question on stdin, answer on
/// stdout, read-only — an agent started inside a vault must not edit it, and Claude Code skips the
/// project settings so the vault's own session hooks (commits, status) do not fire on every question.
public struct AgentPreset: Equatable {
    public let name: String
    public let binary: String
    public let command: String

    public static let known: [AgentPreset] = [
        AgentPreset(name: "Claude Code", binary: "claude",
                    command: "claude -p --tools Read,Grep,Glob --setting-sources user --no-session-persistence"),
        AgentPreset(name: "Codex", binary: "codex",
                    command: "codex exec --skip-git-repo-check --ephemeral --sandbox read-only -"),
    ]

    /// Known agents whose binary is an executable somewhere on `path` (colon-separated).
    public static func discover(path: String?) -> [AgentPreset] {
        let dirs = (path ?? ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        return known.filter { preset in
            dirs.contains { FileManager.default.isExecutableFile(atPath: ($0 as NSString).appendingPathComponent(preset.binary)) }
        }
    }

    /// The preset a saved command came from; nil — the owner's own command.
    public static func matching(_ command: String) -> AgentPreset? {
        let c = command.trimmingCharacters(in: .whitespaces)
        return known.first { $0.command == c }
    }

    /// What to call the agent in the window and in the meeting file: the preset's name, else the
    /// first word of the command.
    public static func displayName(for command: String) -> String {
        if let p = matching(command) { return p.name }
        let first = command.trimmingCharacters(in: .whitespaces).split(separator: " ").first.map(String.init) ?? "агент"
        return (first as NSString).lastPathComponent
    }
}
