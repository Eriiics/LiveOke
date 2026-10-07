// El modo multihilo debe producir exactamente el mismo audio que el modo normal.
#include "../src/Mixer.h"
#include <cstdio>
using namespace vc;

static SlotPtr fx (const char* t) { auto s = std::make_shared<Slot>(); s->uid = t; s->type = t; s->fx = createBuiltin (t); s->fx->prepare (48000, kMaxBlock); return s; }

static void build (Mixer& mx)
{
    auto p = std::make_unique<Patch>();
    for (int i = 0; i < 3; ++i)
    {
        auto s = std::make_unique<PatchStrip>();
        s->id = "in" + juce::String (i); s->sourceKind = 1; s->channels = { i % 2 };
        s->st = std::make_shared<StripState>(); s->st->gainSmooth.reset (48000, 0.02); s->st->gainSmooth.setCurrentAndTargetValue (1.0f);
        s->chain = { fx ("eq"), fx ("compressor"), fx ("chorus"), fx ("reverb") };
        p->strips.push_back (std::move (s));
    }
    p->numInputs = 3;
    mx.setPatch (std::move (p));
    mx.prepareOutputs (48000, 32);
    mx.running.store (true);
}

int main()
{
    Mixer a, b;
    build (a); build (b);
    b.startWorkers (3);
    b.multithread.store (true);
    ClockBridge br; br.configure (48000, 48000, 64);
    const int N = 32;
    std::vector<float> i0 (N), i1 (N), oa0 (N), oa1 (N), ob0 (N), ob1 (N);
    const float* ins[2] { i0.data(), i1.data() };
    float* outA[2] { oa0.data(), oa1.data() }; float* outB[2] { ob0.data(), ob1.data() };
    double maxDiff = 0, ta = 0, tb = 0; juce::Random rnd (7);
    for (int blk = 0; blk < 48000 * 4 / N; ++blk)
    {
        for (int i = 0; i < N; ++i) { i0[i] = rnd.nextFloat() - 0.5f; i1[i] = 0.3f * std::sin (0.05f * (blk * N + i)); }
        auto t0 = juce::Time::getHighResolutionTicks();
        a.process (ins, 2, outA, 2, N, false, br, nullptr);
        auto t1 = juce::Time::getHighResolutionTicks();
        b.process (ins, 2, outB, 2, N, false, br, nullptr);
        auto t2 = juce::Time::getHighResolutionTicks();
        ta += juce::Time::highResolutionTicksToSeconds (t1 - t0); tb += juce::Time::highResolutionTicksToSeconds (t2 - t1);
        for (int i = 0; i < N; ++i) maxDiff = std::max (maxDiff, (double) std::abs (oa0[i] - ob0[i]) + std::abs (oa1[i] - ob1[i]));
    }
    const int blocks = 48000 * 4 / N;
    std::printf ("diferencia max=%g | normal %.1f us/bloque, multihilo %.1f us/bloque (paralelo=%d)\n",
                 maxDiff, ta * 1e6 / blocks, tb * 1e6 / blocks, (int) b.lastParallel.load());
    b.stopWorkers();
    const bool pass = maxDiff == 0.0 && b.lastParallel.load();
    std::printf (pass ? "PASS\n" : "FAIL\n");
    return pass ? 0 : 1;
}
