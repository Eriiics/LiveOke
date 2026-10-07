#include "Recorder.h"

Recorder::Recorder() : juce::Thread ("VocalChain Recorder") {}

Recorder::~Recorder()
{
    if (isRecording() || isThreadRunning())
        stop();
}

void Recorder::prepare (double sampleRate, double fifoSeconds)
{
    jassert (! isRecording());
    sr = sampleRate > 0 ? sampleRate : 48000.0;
    const int size = juce::jmax (8192, (int) (sr * fifoSeconds));
    fifo.setTotalSize (size);
    bufL.assign ((size_t) size, 0.0f);
    bufR.assign ((size_t) size, 0.0f);
}

bool Recorder::start (const juce::File& wavFile, juce::String& error)
{
    if (isRecording()) { error = juce::String::fromUTF8 ("ya está grabando"); return false; }
    if (bufL.empty()) prepare (sr);

    if (! wavFile.getParentDirectory().createDirectory()) { error = "no se pudo crear la carpeta"; return false; }
    if (wavFile.exists()) { error = "el archivo ya existe (no se sobrescribe): " + wavFile.getFullPathName(); return false; }

    auto stream = std::unique_ptr<juce::OutputStream> (wavFile.createOutputStream (1 << 16));
    if (stream == nullptr) { error = "no se pudo abrir " + wavFile.getFullPathName(); return false; }

    juce::WavAudioFormat wav;
    auto* w = wav.createWriterFor (stream.get(), sr, 2, 32, {}, 0);
    if (w == nullptr) { error = juce::String::fromUTF8 ("WavAudioFormat no creó el escritor"); return false; }
    stream.release();  // ahora es del writer
    writer.reset (w);

    fifo.reset();
    framesPushed.store (0);
    dropped.store (0);
    framesWritten = 0;
    writeError = false;
    lastFlushMs = juce::Time::getMillisecondCounter();
    { std::lock_guard<std::mutex> g (markerLock); markers.clear(); }
    currentFile = wavFile;

    startThread (juce::Thread::Priority::normal);
    armed.store (true, std::memory_order_release);
    return true;
}

double Recorder::elapsedSeconds() const noexcept
{
    return (double) framesPushed.load (std::memory_order_relaxed) / sr;
}

double Recorder::marker (const juce::String& name)
{
    const double t = elapsedSeconds();
    std::lock_guard<std::mutex> g (markerLock);
    markers.push_back ({ t, name });
    return t;
}

void Recorder::pushBlock (const float* left, const float* right, int n) noexcept
{
    if (n <= 0) return;
    pushing.store (true, std::memory_order_seq_cst);
    if (armed.load (std::memory_order_seq_cst))
    {
        if (right == nullptr) right = left;
        int s1, n1, s2, n2;
        fifo.prepareToWrite (n, s1, n1, s2, n2);
        if (n1 + n2 < n)
        {
            dropped.fetch_add (n, std::memory_order_relaxed);
        }
        else
        {
            std::memcpy (bufL.data() + s1, left, sizeof (float) * (size_t) n1);
            std::memcpy (bufR.data() + s1, right, sizeof (float) * (size_t) n1);
            if (n2 > 0)
            {
                std::memcpy (bufL.data() + s2, left + n1, sizeof (float) * (size_t) n2);
                std::memcpy (bufR.data() + s2, right + n1, sizeof (float) * (size_t) n2);
            }
            fifo.finishedWrite (n1 + n2);
        }
        framesPushed.fetch_add (n, std::memory_order_relaxed);
    }
    pushing.store (false, std::memory_order_release);
}

void Recorder::drain (bool all)
{
    for (;;)
    {
        const int ready = fifo.getNumReady();
        if (ready <= 0) break;
        int s1, n1, s2, n2;
        fifo.prepareToRead (ready, s1, n1, s2, n2);
        if (writer != nullptr && ! writeError)
        {
            const float* a[2] = { bufL.data() + s1, bufR.data() + s1 };
            if (n1 > 0 && ! writer->writeFromFloatArrays (a, 2, n1)) writeError = true;
            const float* b[2] = { bufL.data() + s2, bufR.data() + s2 };
            if (n2 > 0 && ! writer->writeFromFloatArrays (b, 2, n2)) writeError = true;
        }
        fifo.finishedRead (n1 + n2);
        framesWritten += n1 + n2;
        if (! all) break;
    }
}

void Recorder::run()
{
    while (! threadShouldExit())
    {
        drain (false);
        const auto now = juce::Time::getMillisecondCounter();
        if (writer != nullptr && now - lastFlushMs > 2000)
        {
            writer->flush();
            lastFlushMs = now;
        }
        wait (5);
    }
    drain (true);
}

Recorder::Result Recorder::stop()
{
    Result r;
    if (! isRecording() && ! isThreadRunning()) { r.error = juce::String::fromUTF8 ("no está grabando"); return r; }

    armed.store (false, std::memory_order_seq_cst);
    for (int i = 0; pushing.load (std::memory_order_acquire) && i < 100000; ++i)
        juce::Thread::yield();

    signalThreadShouldExit();
    notify();
    stopThread (4000);
    drain (true);

    if (writer != nullptr) writer->flush();
    writer.reset();

    r.ok = ! writeError;
    r.frames = framesWritten;
    r.seconds = (double) framesWritten / sr;
    r.dropped = dropped.load();
    r.path = currentFile.getFullPathName();
    if (writeError) r.error = juce::String::fromUTF8 ("error escribiendo el WAV (¿disco lleno?)");
    {
        std::lock_guard<std::mutex> g (markerLock);
        r.markers = markers;
    }
    return r;
}
