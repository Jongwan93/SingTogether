import AppKit

/// GitHub Releases의 최신 태그와 이 앱의 버전을 비교한다. 시작할 때 하루 한 번(설정에서 끌 수 있다), 메뉴에서 바로.
/// GitHub에는 버전을 묻는 요청 하나만 간다 (악보도, 사용자 정보도 없이).
enum UpdateCheck {
    static let repo = "Jongwan93/SingTogether"
    private static let api = URL(string: "https://api.github.com/repos/\(repo)/releases/latest")!

    struct Release {
        let version: String
        let page: URL          // 릴리스 노트와 내려받기
    }

    struct Failure: LocalizedError {
        let errorDescription: String?
        init(_ s: String) { errorDescription = s }
    }

    static var current: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0" }

    /// "1.10" > "1.9": 점으로 나눈 숫자 비교
    static func isNewer(_ a: String, than b: String) -> Bool {
        let pa = a.split(separator: ".").map { Int($0) ?? 0 }, pb = b.split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(pa.count, pb.count) {
            let x = i < pa.count ? pa[i] : 0, y = i < pb.count ? pb[i] : 0
            if x != y { return x > y }
        }
        return false
    }

    static func fetch(_ done: @escaping (Result<Release, Error>) -> Void) {
        var req = URLRequest(url: api)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        req.setValue("SingTogether/\(current)", forHTTPHeaderField: "User-Agent")
        req.timeoutInterval = 15
        URLSession.shared.dataTask(with: req) { data, response, error in
            if let error { done(.failure(error)); return }
            guard let data, let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let tag = dict["tag_name"] as? String,
                  let page = (dict["html_url"] as? String).flatMap(URL.init(string:)) else {
                let code = (response as? HTTPURLResponse).map { " (HTTP \($0.statusCode))" } ?? ""
                done(.failure(Failure("Unexpected answer from GitHub" + code)))
                return
            }
            let version = tag.hasPrefix("v") ? String(tag.dropFirst()) : tag
            done(.success(Release(version: version, page: page)))
        }.resume()
    }
}
