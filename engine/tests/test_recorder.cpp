// Prueba del Recorder: un hilo "ASIO" falso empuja bloques de 16 muestras a 8x tiempo real mientras otro
// hilo pone marcas; al final se relee el WAV y se verifica que no falte ni sobre ninguna muestra.
#include "../src/Recorder.h"
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>

static float sample (juce::int64 i, int ch) { return (float) ((i * 7 + ch * 3) % 2000) / 2000.0f - 0.5f; }

int main()
{
    const double sr = 48000.0;
    const int block = 16;
    const juce::int64 total = (juce::int64) (sr * 6.0);

    Recorder rec;
    rec.prepare (sr);
    auto f = juce::File::getSpecialLocation (juce::File::tempDirectory).getChildFile ("vc_rec_test/grabacion.tmp.wav");
    f.getParentDirectory().deleteRecursively();
    juce::String err;
    if (! rec.start (f, err)) { std::printf ("start FAIL: %s\n", err.toRawUTF8()); return 1; }

    double worstPushUs = 0;
    std::thread audio ([&] {
        float L[block], R[block];
        auto t0 = std::chrono::steady_clock::now();
        for (juce::int64 pos = 0; pos < total; pos += block)
        {
            for (int i = 0; i < block; ++i) { L[i] = sample (pos + i, 0); R[i] = sample (pos + i, 1); }
            auto a = std::chrono::steady_clock::now();
            rec.pushBlock (L, R, block);
            auto us = std::chrono::duration<double, std::micro> (std::chrono::steady_clock::now() - a).count();
            if (us > worstPushUs) worstPushUs = us;
            std::this_thread::sleep_until (t0 + std::chrono::microseconds ((juce::int64) ((pos + block) / sr * 1e6 / 8)));
        }
    });
    std::this_thread::sleep_for (std::chrono::milliseconds (200));
    double t1 = rec.marker ("Beat 2");
    std::this_thread::sleep_for (std::chrono::milliseconds (300));
    double t2 = rec.marker ("Beat 3");
    audio.join();
    auto r = rec.stop();

    std::printf ("ok=%d frames=%lld seconds=%.3f dropped=%lld markers=%d (%.2f, %.2f) worstPush=%.1fus\n",
                 (int) r.ok, (long long) r.frames, r.seconds, (long long) r.dropped, (int) r.markers.size(), t1, t2, worstPushUs);

    juce::AudioFormatManager fm;
    fm.registerBasicFormats();
    std::unique_ptr<juce::AudioFormatReader> rd (fm.createReaderFor (f));
    if (rd == nullptr) { std::printf ("no se pudo leer el WAV\n"); return 1; }
    juce::AudioBuffer<float> buf (2, (int) rd->lengthInSamples);
    rd->read (&buf, 0, (int) rd->lengthInSamples, 0, true, true);
    int bad = 0;
    for (int i = 0; i < buf.getNumSamples(); ++i)
        for (int c = 0; c < 2; ++c)
            if (buf.getSample (c, i) != sample (i, c)) ++bad;
    std::printf ("wav: len=%lld bits=%d float=%d sr=%.0f bad=%d\n", (long long) rd->lengthInSamples,
                 (int) rd->bitsPerSample, (int) rd->usesFloatingPointData, rd->sampleRate, bad);
    const bool fmtOk = rd->usesFloatingPointData && rd->lengthInSamples == total;
    rd.reset();

    if (! rec.start (f.getSiblingFile ("toma2.tmp.wav"), err)) { std::printf ("restart FAIL\n"); return 1; }
    float z[block] = {};
    for (int i = 0; i < 100; ++i) rec.pushBlock (z, nullptr, block);
    auto r2 = rec.stop();
    std::printf ("toma2 frames=%lld\n", (long long) r2.frames);

    const bool pass = r.ok && r.frames == total && r.dropped == 0 && bad == 0 && fmtOk
                      && r.markers.size() == 2 && t1 > 0 && t2 > t1 && r2.frames == 1600;
    std::printf (pass ? "PASS\n" : "FAIL\n");
    f.getParentDirectory().deleteRecursively();
    return pass ? 0 : 1;
}
