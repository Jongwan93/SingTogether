import AVFoundation
import Transpose

/// 확인 창의 멜로디 재생: macOS에 들어 있는 GM 음원(gs_instruments.dls)의 피아노로 음표를 차례로 울린다.
/// 사용자가 ▶를 눌렀을 때만 소리가 나고, 울리는 음표마다 onEvent로 알려 그림 위에 표시한다.
final class MelodyPlayer {
    static let shared = MelodyPlayer()

    private let engine = AVAudioEngine()
    private let sampler = AVAudioUnitSampler()
    private var started = false
    private var generation = 0                  // 재생을 멈추면 올라간다: 예약된 다음 음표가 자기 세대가 아니면 울리지 않는다
    private var sounding: UInt8?
    private(set) var isPlaying = false
    var onEvent: ((CompactText.Event?) -> Void)?   // 울리기 시작한 음표. nil = 끝남

    /// 분당 4분음표 수. 확인 창의 템포 메뉴가 바꾸고 UserDefaults에 남는다.
    static var tempo: Double {
        get { let v = UserDefaults.standard.double(forKey: "playTempo"); return v > 0 ? v : 100 }
        set { UserDefaults.standard.set(newValue, forKey: "playTempo") }
    }
    static let tempos: [Double] = [60, 80, 100, 120, 140]

    private init() {
        engine.attach(sampler)
        engine.connect(sampler, to: engine.mainMixerNode, format: nil)
        let bank = URL(fileURLWithPath: "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls")
        try? sampler.loadSoundBankInstrument(at: bank, program: 0, bankMSB: UInt8(kAUSampler_DefaultMelodicBankMSB), bankLSB: UInt8(kAUSampler_DefaultBankLSB))
    }

    private func start() -> Bool {
        if !started {
            do { try engine.start(); started = true } catch { return false }
        }
        return true
    }

    /// 음 하나 (클릭한 음표, 고친 음표). 재생 중이면 그것을 멈춘다.
    func play(midi: Int, seconds: Double = 0.6) {
        stop()
        guard start() else { return }
        let n = UInt8(clamping: midi)
        sampler.startNote(n, withVelocity: 90, onChannel: 0)
        sounding = n
        let g = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self, self.generation == g, self.sounding == n else { return }
            self.sampler.stopNote(n, onChannel: 0)
            self.sounding = nil
        }
    }

    /// 음표 열을 차례로. 붙임줄로 이어진 같은 음은 다시 치지 않는다.
    func play(_ events: [CompactText.Event], tempo: Double = MelodyPlayer.tempo) {
        stop()
        guard !events.isEmpty, start() else { return }
        isPlaying = true
        let g = generation
        let beat = 60 / max(20, tempo)
        step(events, at: 0, time: .now(), beat: beat, generation: g, continuing: false)
    }

    private func step(_ events: [CompactText.Event], at k: Int, time: DispatchTime, beat: Double, generation g: Int, continuing: Bool) {
        guard g == generation else { return }
        guard k < events.count else { finish(); return }
        let e = events[k]
        let n = e.midi.map { UInt8(clamping: $0) }
        if !continuing {
            if let s = sounding { sampler.stopNote(s, onChannel: 0); sounding = nil }
            if let n { sampler.startNote(n, withVelocity: 90, onChannel: 0); sounding = n }
        }
        onEvent?(e)
        let next = time + e.quarters * beat
        let tied = e.tie && k + 1 < events.count && events[k + 1].midi == e.midi && e.midi != nil
        DispatchQueue.main.asyncAfter(deadline: next) { [weak self] in
            self?.step(events, at: k + 1, time: next, beat: beat, generation: g, continuing: tied)
        }
    }

    private func finish() {
        if let s = sounding { sampler.stopNote(s, onChannel: 0); sounding = nil }
        isPlaying = false
        onEvent?(nil)
    }

    func stop() {
        generation += 1
        if let s = sounding { sampler.stopNote(s, onChannel: 0); sounding = nil }
        if isPlaying {
            isPlaying = false
            onEvent?(nil)
        }
    }
}
