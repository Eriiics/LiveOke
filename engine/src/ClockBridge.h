// Puente entre dos dispositivos con relojes distintos (ej. VB-Cable → Focusrite).
// Un hilo escribe, otro lee re-muestreando con una razón que se corrige sola
// según cuánto audio hay acumulado: así no se acumula latencia ni hay cortes.
#pragma once

#include "JuceConfig.h"
#include <juce_audio_basics/juce_audio_basics.h>
#include <atomic>
#include <cmath>
#include <vector>

namespace vc
{
class ClockBridge
{
public:
    ClockBridge() : fifo (kCapacity) { buf.setSize (2, kCapacity); tmp.setSize (2, kMaxRead); }

    /** Configura las frecuencias y el colchón objetivo (en muestras del lado productor). */
    void configure (double producerRate, double consumerRate, int targetFrames)
    {
        baseRatio = producerRate > 0 && consumerRate > 0 ? producerRate / consumerRate : 1.0;
        target = juce::jlimit (32, kCapacity / 2, targetFrames);
        resetRequested.store (true);
    }

    double latencyMs (double producerRate) const
    {
        return producerRate > 0 ? 1000.0 * smoothedFill.load() / producerRate : 0.0;
    }

    // -- productor ---------------------------------------------------------
    void push (const float* const* ch, int numCh, int n)
    {
        if (numCh <= 0 || n <= 0) return;
        int s1, n1, s2, n2;
        if (fifo.getFreeSpace() < n) { overruns.fetch_add (1); return; }
        fifo.prepareToWrite (n, s1, n1, s2, n2);
        for (int c = 0; c < 2; ++c)
        {
            const float* src = ch[std::min (c, numCh - 1)];
            if (src == nullptr) { buf.clear (c, s1, n1); if (n2) buf.clear (c, s2, n2); continue; }
            buf.copyFrom (c, s1, src, n1);
            if (n2 > 0) buf.copyFrom (c, s2, src + n1, n2);
        }
        fifo.finishedWrite (n1 + n2);
    }

    // -- consumidor --------------------------------------------------------
    /** Rellena L/R con n muestras. Devuelve false si no había audio (sale silencio). */
    bool pull (float* L, float* R, int n)
    {
        if (resetRequested.exchange (false))
        {
            primed = false; integ = 0; interpL.reset(); interpR.reset();
            fifo.finishedRead (fifo.getNumReady());
        }
        int ready = fifo.getNumReady();
        smoothedFill.store (smoothedFill.load() * 0.98 + 0.02 * ready);

        if (! primed)
        {
            if (ready < target) { silence (L, R, n); return false; }
            primed = true;
            smoothedFill.store (ready);
        }
        // Demasiado acumulado (ej. el consumidor estuvo detenido): saltar al objetivo
        if (ready > target * 4)
        {
            fifo.finishedRead (ready - target);
            ready = target;
            interpL.reset(); interpR.reset();
        }

        // Control PI sobre el nivel de llenado → pequeña corrección de la razón
        const double err = (smoothedFill.load() - target) / (double) target;
        integ = juce::jlimit (-0.5, 0.5, integ + err * 0.0005);
        const double corr = juce::jlimit (-0.004, 0.004, err * 0.0015 + integ * 0.002);
        const double ratio = baseRatio * (1.0 + corr);
        currentRatio.store (ratio);

        const int need = std::min (kMaxRead - 8, (int) std::ceil (n * ratio) + 6);
        if (ready < need)
        {
            underruns.fetch_add (1);
            primed = false; interpL.reset(); interpR.reset();
            silence (L, R, n);
            return false;
        }
        int s1, n1, s2, n2;
        fifo.prepareToRead (need, s1, n1, s2, n2);
        for (int c = 0; c < 2; ++c)
        {
            tmp.copyFrom (c, 0, buf, c, s1, n1);
            if (n2 > 0) tmp.copyFrom (c, n1, buf, c, s2, n2);
        }
        const int used = interpL.process (ratio, tmp.getReadPointer (0), L, n);
        interpR.process (ratio, tmp.getReadPointer (1), R, n);
        fifo.finishedRead (std::min (used, n1 + n2));
        return true;
    }

    std::atomic<int> underruns { 0 }, overruns { 0 };
    std::atomic<double> currentRatio { 1.0 };

private:
    static void silence (float* L, float* R, int n)
    {
        juce::FloatVectorOperations::clear (L, n);
        juce::FloatVectorOperations::clear (R, n);
    }

    static constexpr int kCapacity = 32768;
    static constexpr int kMaxRead = 16384;
    juce::AbstractFifo fifo;
    juce::AudioBuffer<float> buf, tmp;
    juce::LagrangeInterpolator interpL, interpR;
    double baseRatio = 1.0, integ = 0;
    int target = 512;
    bool primed = false;
    std::atomic<double> smoothedFill { 0 };
    std::atomic<bool> resetRequested { true };
};
} // namespace vc
