# spike_win_stt_sapi.ps1 - Windows built-in speech recognition (SAPI desktop recognizer, offline) as an STT helper.
# Measurement only (Step 1, spikes/WINDOWS_STT.md). No extra packages: Windows PowerShell 5.1 + System.Speech.
#
# Speaks the same contract as the macOS stt_helper (docs/REQUIREMENTS.md FR-2):
#   stdout, one JSON per line: {"type":"ready"|"partial"|"final"|"bye", "channel", "text", "start_s", "end_s", "t_ms", "fed_s"}
#   diagnostics are JSON lines with "phase" (feed_start / speech_detected / rejected / feed_done)
# Feeds a 16 kHz mono 16-bit WAV in real time (x Pace) through a blocking stream, then TailS seconds of silence.
# SAPI has no "finalize through now" call; finals come only from its own end-of-speech timeout.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File spikes\spike_win_stt_sapi.ps1 -Wav <file.wav> [-Pace 1.0] [-Channel system] [-TailS 1.5]
# Needs the ja-JP desktop recognizer (Settings > Time & language > Speech, "MS-1041-80-DESK").
param(
    [Parameter(Mandatory = $true)][string]$Wav,
    [double]$Pace = 1.0,
    [string]$Channel = "system",
    [double]$TailS = 1.5
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Speech

$src = @"
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Speech.AudioFormat;
using System.Speech.Recognition;
using System.Text;
using System.Threading;

public static class Out {
    static readonly object Gate = new object();
    public static long NowMs() { return (long)(DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalMilliseconds; }
    public static string Num(double v) { return Math.Round(v, 3).ToString("0.###", CultureInfo.InvariantCulture); }
    public static string Str(string s) {
        var b = new StringBuilder("\"");
        foreach (char c in s ?? "") {
            if (c == '"') b.Append("\\\"");
            else if (c == '\\') b.Append("\\\\");
            else if (c < ' ') b.Append("\\u" + ((int)c).ToString("x4"));
            else b.Append(c);
        }
        return b.Append('"').ToString();
    }
    // body: already-formatted "key":value pairs without braces
    public static void Line(string body) {
        lock (Gate) {
            Console.Out.WriteLine("{" + body + ",\"t_ms\":" + NowMs() + "}");
            Console.Out.Flush();
        }
    }
}

// Blocking PCM stream: hands SAPI the audio no faster than real time (x pace), then tail silence, then EOF.
public class PacedPcmStream : Stream {
    readonly byte[] pcm;
    readonly long total;
    readonly double bytesPerSec;
    long pos;
    Stopwatch sw;
    public long T0Ms;
    public PacedPcmStream(byte[] pcm, double tailS, double pace) {
        this.pcm = pcm;
        long tail = (long)(tailS * 32000) & ~1L;
        total = pcm.Length + tail;
        bytesPerSec = 32000.0 * pace;
    }
    public double FedS { get { return Interlocked.Read(ref pos) / 32000.0; } }
    public double AudioS { get { return pcm.Length / 32000.0; } }
    public override int Read(byte[] buffer, int offset, int count) {
        if (sw == null) {
            sw = Stopwatch.StartNew();
            T0Ms = Out.NowMs();
            Out.Line("\"phase\":\"feed_start\",\"t0_ms\":" + T0Ms);
        }
        long p = Interlocked.Read(ref pos);
        if (p >= total) return 0;
        int n = (int)Math.Min((long)count, total - p);
        while (sw.Elapsed.TotalSeconds * bytesPerSec < p + n) Thread.Sleep(5);
        for (int i = 0; i < n; i++) {
            long k = p + i;
            buffer[offset + i] = k < pcm.Length ? pcm[k] : (byte)0;
        }
        Interlocked.Add(ref pos, n);
        if (p < pcm.Length && p + n >= pcm.Length)
            Out.Line("\"phase\":\"feed_done\",\"audio_s\":" + Out.Num(AudioS) + ",\"wall_ms\":" + (long)sw.Elapsed.TotalMilliseconds);
        return n;
    }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return false; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return total; } }
    // SAPI asks where it is (Seek(0, Current) / Position); only answer that, never rewind.
    public override long Position { get { return Interlocked.Read(ref pos); } set { Seek(value, SeekOrigin.Begin); } }
    public override void Flush() { }
    public override long Seek(long o, SeekOrigin s) {
        long p = Interlocked.Read(ref pos);
        if ((s == SeekOrigin.Current && o == 0) || (s == SeekOrigin.Begin && o == p)) return p;
        Out.Line("\"phase\":\"seek_refused\",\"offset\":" + o + ",\"origin\":" + Out.Str(s.ToString()));
        throw new NotSupportedException();
    }
    public override void SetLength(long v) { throw new NotSupportedException(); }
    public override void Write(byte[] b, int o, int c) { throw new NotSupportedException(); }
}

public static class SapiSpike {
    static byte[] ReadPcm(string path) {
        byte[] all = File.ReadAllBytes(path);
        int i = 12;
        while (i + 8 <= all.Length) {
            string id = Encoding.ASCII.GetString(all, i, 4);
            int size = BitConverter.ToInt32(all, i + 4);
            if (id == "fmt ") {
                int ch = BitConverter.ToInt16(all, i + 10);
                int hz = BitConverter.ToInt32(all, i + 12);
                int bits = BitConverter.ToInt16(all, i + 22);
                if (ch != 1 || hz != 16000 || bits != 16) throw new Exception("need 16 kHz mono 16-bit wav");
            }
            if (id == "data") {
                int n = Math.Min(size, all.Length - (i + 8));
                byte[] d = new byte[n];
                Buffer.BlockCopy(all, i + 8, d, 0, n);
                return d;
            }
            i += 8 + size + (size & 1);
        }
        throw new Exception("no data chunk");
    }

    static string Range(RecognitionResult r, PacedPcmStream s) {
        double start = 0;
        double end = s.FedS;
        if (r != null && r.Audio != null) {
            start = r.Audio.AudioPosition.TotalSeconds;
            end = start + r.Audio.Duration.TotalSeconds;
        }
        return "\"start_s\":" + Out.Num(start) + ",\"end_s\":" + Out.Num(end);
    }

    public static int Run(string wav, double pace, string channel, double tailS) {
        byte[] pcm = ReadPcm(wav);
        var stream = new PacedPcmStream(pcm, tailS, pace);
        var done = new ManualResetEvent(false);
        var tLoad = Stopwatch.StartNew();
        using (var eng = new SpeechRecognitionEngine(new CultureInfo("ja-JP"))) {
            eng.LoadGrammar(new DictationGrammar());
            eng.SetInputToAudioStream(stream, new SpeechAudioFormatInfo(16000, AudioBitsPerSample.Sixteen, AudioChannel.Mono));
            Out.Line("\"phase\":\"load\",\"ms\":" + tLoad.ElapsedMilliseconds + ",\"recognizer\":" + Out.Str(eng.RecognizerInfo.Name)
                     + ",\"end_silence_ms\":" + (long)eng.EndSilenceTimeout.TotalMilliseconds
                     + ",\"end_silence_ambiguous_ms\":" + (long)eng.EndSilenceTimeoutAmbiguous.TotalMilliseconds);
            string pre = "\"channel\":" + Out.Str(channel);
            eng.SpeechDetected += (o, e) => Out.Line("\"phase\":\"speech_detected\",\"audio_s\":" + Out.Num(e.AudioPosition.TotalSeconds));
            eng.SpeechHypothesized += (o, e) => Out.Line("\"type\":\"partial\"," + pre + ",\"text\":" + Out.Str(e.Result.Text) + ","
                + Range(e.Result, stream) + ",\"fed_s\":" + Out.Num(stream.FedS) + ",\"conf\":" + Out.Num(e.Result.Confidence));
            eng.SpeechRecognized += (o, e) => Out.Line("\"type\":\"final\"," + pre + ",\"text\":" + Out.Str(e.Result.Text) + ","
                + Range(e.Result, stream) + ",\"fed_s\":" + Out.Num(stream.FedS) + ",\"conf\":" + Out.Num(e.Result.Confidence));
            eng.SpeechRecognitionRejected += (o, e) => {
                string t = e.Result != null ? e.Result.Text : "";
                Out.Line("\"phase\":\"rejected\",\"text\":" + Out.Str(t) + ",\"fed_s\":" + Out.Num(stream.FedS));
            };
            eng.RecognizeCompleted += (o, e) => {
                if (e.Error != null) Out.Line("\"phase\":\"error\",\"error\":" + Out.Str(e.Error.Message));
                done.Set();
            };
            Out.Line("\"type\":\"ready\",\"locale\":\"ja-JP\"," + pre + ",\"dst_hz\":16000,\"source\":" + Out.Str("file:" + wav)
                     + ",\"pace\":" + Out.Num(pace) + ",\"engine\":\"sapi\"");
            eng.RecognizeAsync(RecognizeMode.Multiple);
            done.WaitOne();
        }
        Out.Line("\"type\":\"bye\"");
        return 0;
    }
}
"@
Add-Type -TypeDefinition $src -ReferencedAssemblies System.Speech
$full = (Resolve-Path -LiteralPath $Wav).Path
exit [SapiSpike]::Run($full, $Pace, $Channel, $TailS)
