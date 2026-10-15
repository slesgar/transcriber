import Foundation

/// Единственное место, знающее, где лежат данные установки: конфиги, сессии, голоса. Каталог
/// задаёт переменная `TRANSCRIBER_DATA` (ведущая `~` раскрывается), иначе `~/Library/Application Support/Transcriber`.
/// Свойства вычисляемые: переменную окружения можно поменять в тесте и сразу увидеть эффект.
public enum AppPaths {
    public static let dataDirEnvironment = "TRANSCRIBER_DATA"

    public static var dataDir: URL {
        if let value = ProcessInfo.processInfo.environment[dataDirEnvironment], !value.isEmpty {
            return URL(fileURLWithPath: (value as NSString).expandingTildeInPath)   // `~/…` как у воркера
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Transcriber")
    }

    /// Настройки приложения: правила приватности, `talk`, `recon`.
    public static var appConfig: URL { dataDir.appendingPathComponent("app-config.json") }
    /// Каталог сессий: протокол между приложением и воркером.
    public static var sessions: URL { dataDir.appendingPathComponent("sessions") }
    /// Настройки воркера.
    public static var workerConfig: URL { dataDir.appendingPathComponent("config.yaml") }
}
