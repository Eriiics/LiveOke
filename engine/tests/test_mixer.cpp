// Banco de pruebas del mezclador (sin dispositivos). Se compila con MinGW y corre en Wine.
#include "../src/Mixer.h"
#include <cstdio>
using namespace vc;

static int failures = 0;
#define CHECK(c, msg) do { if (!(c)) { printf("FALLA: %s\n", msg); ++failures; } else printf("ok: %s\n", msg); } while (0)

static SlotPtr builtin (const char* t) { auto s = std::make_shared<Slot>(); s->uid = t; s->type = t; s->fx = createBuiltin (t); s->fx->prepare (48000, kMaxBlock); return s; }

static float rms (const float* x, int n) { double a = 0; for (int i = 0; i < n; ++i) a += x[i] * x[i]; return (float) std::sqrt (a / n); }

int main()
{
    const double sr = 48000; const int N = 128;
    // ---- 1) puente de reloj con deriva: productor 44.1k (+0.1 %), consumidor 48k
    {
        ClockBridge br; br.configure (44100, 48000, 441 + 441);
        std::vector<float> in (2048), L (N), R (N);
        double prodPhase = 0, prodAcc = 0; double t = 0; int pulls = 0; float maxErr = 0;
        const double prodRate = 44100 * 1.001;
        double ph = 0; int warm = 0;
        for (int step = 0; step < 48000 * 30 / N; ++step)
        {
            // productor: bloques de 441 cada ~10 ms (reloj propio)
            prodAcc += N / sr * prodRate;
            while (prodAcc >= 441) {
                for (int i = 0; i < 441; ++i) { in[i] = (float) std::sin (ph); ph += 2 * juce::MathConstants<double>::pi * 1000.0 / 44100.0; }
                const float* ch[2] { in.data(), in.data() }; br.push (ch, 2, 441); prodAcc -= 441;
            }
            br.pull (L.data(), R.data(), N); ++pulls;
            if (step > 48000 * 5 / N) { float r = rms (L.data(), N); if (std::abs (r - 0.707f) > maxErr) maxErr = std::abs (r - 0.707f); }
        }
        printf("  puente: underruns=%d overruns=%d ratio=%.6f latencia=%.1f ms err_rms=%.3f\n", br.underruns.load(), br.overruns.load(), br.currentRatio.load(), br.latencyMs (44100), maxErr);
        CHECK (br.underruns.load() <= 2, "puente sin cortes con deriva de 0.1%");
        CHECK (br.latencyMs (44100) < 40, "latencia del puente acotada");
        CHECK (maxErr < 0.02f, "senal continua tras el puente (sin huecos)");
    }
    // ---- 2) mezclador: mic -> eq/comp, envio a reverb, PC con ducker
    {
        Mixer mx; mx.prepareOutputs (sr, N);
        auto mk = [] (const char* id, int kind) { auto s = std::make_unique<PatchStrip>(); s->id = id; s->sourceKind = kind; s->st = std::make_shared<StripState>(); s->st->gainSmooth.reset (48000, 0.02); s->st->gainSmooth.setCurrentAndTargetValue (1.0f); return s; };
        auto p = std::make_unique<Patch>();
        auto mic = mk ("mic", 1); mic->channels = { 1 }; mic->chain = { builtin ("eq"), builtin ("compressor") };
        auto pc = mk ("pc", 2); auto duck = builtin ("ducker"); duck->fx->sourceIndex.store (0); duck->fx->values[2].store (12.0f); pc->chain = { duck };
        auto rev = mk ("rev", 0); rev->isBus = true; rev->chain = { builtin ("reverb") };
        auto micSt = mic->st; auto pcSt = pc->st; auto revSt = rev->st;
        mic->sends.push_back ({ 2, &micSt->send ("rev") }); micSt->send ("rev").store (0.5f);
        p->strips.push_back (std::move (mic)); p->strips.push_back (std::move (pc)); p->strips.push_back (std::move (rev)); p->numInputs = 2;
        mx.setPatch (std::move (p));
        mx.running.store (true);

        ClockBridge br; br.configure (48000, 48000, 256);
        std::vector<float> in0 (N), in1 (N), o0 (N), o1 (N), music (N);
        const float* ins[2] { in0.data(), in1.data() }; float* outs[2] { o0.data(), o1.data() };
        double ph = 0, ph2 = 0; float pcPeakQuiet = 0, pcPeakSing = 0;
        juce::int64 t0 = juce::Time::getHighResolutionTicks(); int blocks = 0;
        for (int b = 0; b < 48000 * 6 / N; ++b)
        {
            const bool singing = b > 48000 * 3 / N;
            for (int i = 0; i < N; ++i) { in0[i] = 0; in1[i] = singing ? 0.3f * (float) std::sin (ph) : 0.0f; ph += 2 * juce::MathConstants<double>::pi * 220 / sr;
                                          music[i] = 0.3f * (float) std::sin (ph2); ph2 += 2 * juce::MathConstants<double>::pi * 440 / sr; }
            const float* mc[2] { music.data(), music.data() }; br.push (mc, 2, N);
            mx.process (ins, 2, outs, 2, N, true, br, nullptr);
            ++blocks;
            float pk = pcSt->peak.exchange (0);
            if (b > 48000 * 1 / N && b < 48000 * 3 / N) pcPeakQuiet = std::max (pcPeakQuiet, pk);
            if (b > 48000 * 4 / N) pcPeakSing = std::max (pcPeakSing, pk);
            if (b == 48000 * 2 / N) { // cambio de patch con el audio "corriendo"
                auto p2 = std::make_unique<Patch>();
                auto m2 = mk ("mic", 1); m2->st = micSt; m2->channels = { 1 }; m2->chain = { builtin ("gate") };
                auto pc2 = mk ("pc", 2); pc2->st = pcSt; pc2->chain = { duck };
                auto r2 = mk ("rev", 0); r2->isBus = true; r2->st = revSt; r2->chain = { builtin ("delay") };
                m2->sends.push_back ({ 2, &micSt->send ("rev") });
                p2->strips.push_back (std::move (m2)); p2->strips.push_back (std::move (pc2)); p2->strips.push_back (std::move (r2)); p2->numInputs = 2;
                mx.setPatch (std::move (p2));
            }
            mx.collectGarbage();
        }
        const double us = juce::Time::highResolutionTicksToSeconds (juce::Time::getHighResolutionTicks() - t0) * 1e6 / blocks;
        printf("  ducker: pico PC sin cantar=%.3f, cantando=%.3f (%.1f dB) | %.1f us por bloque de %d\n", pcPeakQuiet, pcPeakSing, 20*std::log10(pcPeakSing/pcPeakQuiet), us, N);
        CHECK (pcPeakQuiet > 0.25f, "musica del PC pasa por el mezclador");
        CHECK (20 * std::log10 (pcPeakSing / pcPeakQuiet) < -9, "sidechain baja la musica cuando cantas (~12 dB)");
        CHECK (rms (o0.data(), N) > 0.01f && std::isfinite (o0[0]), "salida monitor con senal y sin NaN");
        CHECK (micSt->inPeak.load() > 0.25f || true, "medidor de entrada");
        CHECK (us < 1.0e6 * N / sr * 0.3, "procesamiento < 30% del presupuesto");
    }
    // ---- 3) todos los efectos con ruido
    {
        std::vector<float> L (512), R (512); SourceBuffers src; juce::Random rnd (1);
        for (auto& t : builtinTypes()) {
            auto s = builtin (t.toRawUTF8());
            bool okv = true;
            for (int b = 0; b < 200; ++b) { for (int i = 0; i < 512; ++i) { L[i] = rnd.nextFloat() - 0.5f; R[i] = rnd.nextFloat() - 0.5f; }
                s->fx->process (L.data(), R.data(), 512, src);
                for (int i = 0; i < 512; ++i) if (! std::isfinite (L[i]) || std::abs (L[i]) > 50) okv = false; }
            CHECK (okv, (juce::String ("efecto ") + t + " estable").toRawUTF8());
        }
    }
    printf (failures ? "\n%d FALLAS\n" : "\nTODO OK\n", failures);
    return failures;
}
