// Mezclador en tiempo real.
//
// El hilo de mensajes arma una "Patch" inmutable (qué canales hay y en qué orden
// van sus efectos) y se la pasa al hilo de audio con un intercambio atómico.
// Los valores que se mueven seguido (faders, envíos, parámetros) son atómicos y
// no requieren rearmar nada. Las Patch viejas se borran en el hilo de mensajes.
#pragma once

#include "JuceConfig.h"
#include <juce_audio_processors/juce_audio_processors.h>

#include "ClockBridge.h"
#include "Dsp.h"

#include <map>
#include <memory>
#include <vector>

namespace vc
{
// ---------------------------------------------------------------------------
/** Reloj simple para los plugins que lo piden (tempo fijo, siempre "tocando"). */
class SimplePlayHead : public juce::AudioPlayHead
{
public:
    juce::Optional<PositionInfo> getPosition() const override
    {
        PositionInfo p;
        p.setBpm (bpm.load());
        p.setTimeSignature (TimeSignature { 4, 4 });
        p.setIsPlaying (true);
        p.setTimeInSamples (samples.load());
        p.setTimeInSeconds (samples.load() / std::max (1.0, rate.load()));
        return p;
    }
    std::atomic<double> bpm { 120.0 }, rate { 48000.0 };
    std::atomic<juce::int64> samples { 0 };
};

// ---------------------------------------------------------------------------
struct PluginSlot
{
    std::unique_ptr<juce::AudioPluginInstance> inst;
    juce::AudioBuffer<float> pbuf;
    juce::MidiBuffer midi;
    juce::String path, name;
    int ins = 2, outs = 2;

    void prepare (double sr, int block)
    {
        inst->releaseResources();
        inst->setRateAndBufferSizeDetails (sr, block);
        inst->prepareToPlay (sr, block);
        ins = inst->getTotalNumInputChannels();
        outs = inst->getTotalNumOutputChannels();
        pbuf.setSize (std::max ({ ins, outs, 2 }), std::max (block, kMaxBlock));
        midi.ensureSize (256);
    }

    void process (float* L, float* R, int n)
    {
        const int nch = std::max (ins, outs);
        juce::AudioBuffer<float> view (pbuf.getArrayOfWritePointers(), std::max (nch, 1), n);
        view.clear();
        if (ins == 1)
        {
            auto* d = view.getWritePointer (0);
            for (int i = 0; i < n; ++i) d[i] = 0.5f * (L[i] + R[i]);
        }
        else if (ins >= 2)
        {
            view.copyFrom (0, 0, L, n);
            view.copyFrom (1, 0, R, n);
        }
        midi.clear();
        inst->processBlock (view, midi);
        if (outs <= 0) return;
        juce::FloatVectorOperations::copy (L, view.getReadPointer (0), n);
        juce::FloatVectorOperations::copy (R, view.getReadPointer (outs >= 2 ? 1 : 0), n);
    }
};

// ---------------------------------------------------------------------------
struct Slot
{
    juce::String uid, type;            // type: tipo nativo ("reverb", ...) o "vst3"
    std::unique_ptr<BuiltinFx> fx;
    std::unique_ptr<PluginSlot> plugin;
    std::atomic<bool> enabled { true };
    std::atomic<bool> failed { false };
    // datos del plugin (se conservan aunque no cargue, para no perder el preset)
    juce::String pluginPath, pluginName, error;
    juce::MemoryBlock savedState;

    bool isPlugin() const { return plugin != nullptr; }
    juce::String title() const
    {
        if (plugin) return plugin->name;
        if (pluginName.isNotEmpty()) return pluginName + " (no cargó)";
        return fx ? juce::String (juce::CharPointer_UTF8 (fx->label())) : type;
    }
};
using SlotPtr = std::shared_ptr<Slot>;

// ---------------------------------------------------------------------------
/** Estado de un canal/bus que sobrevive a los cambios de Patch. */
struct StripState
{
    std::atomic<float> gain { 1.0f }, pan { 0.0f };
    std::atomic<bool> mute { false }, solo { false }, toMonitor { true }, toStream { true };
    // envíos: id de bus → nivel (nunca se borran entradas mientras viva el canal)
    std::map<juce::String, std::unique_ptr<std::atomic<float>>> sends;
    // medidores
    std::atomic<float> peak { 0 }, inPeak { 0 };
    std::atomic<int> clips { 0 };
    // solo hilo de audio
    juce::LinearSmoothedValue<float> gainSmooth;

    std::atomic<float>& send (const juce::String& busId)
    {
        auto& p = sends[busId];
        if (! p) p = std::make_unique<std::atomic<float>> (0.0f);
        return *p;
    }
};
using StripStatePtr = std::shared_ptr<StripState>;

struct OutputState
{
    std::atomic<float> gain { 1.0f };
    std::atomic<bool> mute { false }, limiter { true };
    std::atomic<float> peak { 0 }, prePeak { 0 };
    std::atomic<int> limitHits { 0 };
};

// ---------------------------------------------------------------------------
struct PatchStrip
{
    juce::String id;
    bool isBus = false;
    int sourceKind = 0;              // 0 = nada, 1 = canales ASIO, 2 = captura (PC)
    std::vector<int> channels;       // canales ASIO (mezcla mono si hay varios)
    bool stereoChannels = false;     // con 2 canales: usarlos como L/R (ej. loopback)
    StripStatePtr st;
    std::vector<SlotPtr> chain;
    std::vector<std::pair<int, std::atomic<float>*>> sends; // (posición del bus en la Patch, nivel)
    juce::AudioBuffer<float> raw, work;
};

struct Patch
{
    std::vector<std::unique_ptr<PatchStrip>> strips;   // primero entradas, después buses
    int numInputs = 0;
    SourceBuffers sources;
};

// ---------------------------------------------------------------------------
class Mixer
{
public:
    Mixer()
    {
        for (auto& r : retired) r.store (nullptr);
        monitorBuf.setSize (2, kMaxBlock);
        streamBuf.setSize (2, kMaxBlock);
        pcBuf.setSize (2, kMaxBlock);
    }

    ~Mixer()
    {
        delete current;
        delete pending.exchange (nullptr);
        collectGarbage();
    }

    // -- hilo de mensajes ------------------------------------------------------
    void setPatch (std::unique_ptr<Patch> p)
    {
        for (auto& s : p->strips)
        {
            s->raw.setSize (2, kMaxBlock);
            s->work.setSize (2, kMaxBlock);
        }
        p->sources.count = std::min ((int) p->sources.left.size(), p->numInputs);
        for (int i = 0; i < p->sources.count; ++i)
        {
            p->sources.left[(size_t) i] = p->strips[(size_t) i]->raw.getReadPointer (0);
            p->sources.right[(size_t) i] = p->strips[(size_t) i]->raw.getReadPointer (1);
        }
        if (! running.load())
        {
            // sin audio corriendo: reemplazo directo
            std::unique_ptr<Patch> old (current);
            current = p.release();
            return;
        }
        delete pending.exchange (p.release());
    }

    void collectGarbage()
    {
        for (auto& r : retired)
            if (auto* p = r.exchange (nullptr)) delete p;
    }

    // -- hilo de audio ---------------------------------------------------------
    void process (const float* const* in, int numIn, float* const* out, int numOut, int n, bool pcAvailable,
                  ClockBridge& pcBridge, ClockBridge* streamBridge)
    {
        if (auto* p = pending.exchange (nullptr))
        {
            retire (current);
            current = p;
        }
        for (int c = 0; c < numOut; ++c)
            if (out[c] != nullptr) juce::FloatVectorOperations::clear (out[c], n);
        Patch* patch = current;
        if (patch == nullptr || n > kMaxBlock) return;

        monitorBuf.clear (0, n);
        streamBuf.clear (0, n);
        bool pulledPc = false;

        // 1) señales crudas de entrada
        bool anySolo = false;
        for (int i = 0; i < patch->numInputs; ++i)
        {
            auto& s = *patch->strips[(size_t) i];
            auto* L = s.raw.getWritePointer (0);
            auto* R = s.raw.getWritePointer (1);
            juce::FloatVectorOperations::clear (L, n);
            juce::FloatVectorOperations::clear (R, n);
            if (s.sourceKind == 1)
            {
                if (s.stereoChannels && s.channels.size() == 2)
                {
                    if (s.channels[0] < numIn && in[s.channels[0]]) juce::FloatVectorOperations::copy (L, in[s.channels[0]], n);
                    if (s.channels[1] < numIn && in[s.channels[1]]) juce::FloatVectorOperations::copy (R, in[s.channels[1]], n);
                }
                else
                {
                    int used = 0;
                    for (int ch : s.channels)
                        if (ch >= 0 && ch < numIn && in[ch] != nullptr) { juce::FloatVectorOperations::add (L, in[ch], n); ++used; }
                    if (used > 1) juce::FloatVectorOperations::multiply (L, 1.0f / (float) used, n);
                    juce::FloatVectorOperations::copy (R, L, n);
                }
            }
            else if (s.sourceKind == 2 && pcAvailable)
            {
                if (! pulledPc)
                {
                    pcBridge.pull (pcBuf.getWritePointer (0), pcBuf.getWritePointer (1), n);
                    pulledPc = true;
                }
                juce::FloatVectorOperations::copy (L, pcBuf.getReadPointer (0), n);
                juce::FloatVectorOperations::copy (R, pcBuf.getReadPointer (1), n);
            }
            const float pk = std::max (juce::FloatVectorOperations::findMaximum (L, n) , -juce::FloatVectorOperations::findMinimum (L, n));
            const float pkr = std::max (juce::FloatVectorOperations::findMaximum (R, n), -juce::FloatVectorOperations::findMinimum (R, n));
            const float p2 = std::max (pk, pkr);
            meterMax (s.st->inPeak, p2);
            if (p2 >= 0.98f) s.st->clips.fetch_add (1);
            anySolo = anySolo || s.st->solo.load();
        }
        for (size_t i = (size_t) patch->numInputs; i < patch->strips.size(); ++i)
        {
            auto& s = *patch->strips[i];
            s.raw.clear (0, n);
            anySolo = anySolo || s.st->solo.load();
        }

        // 2) canales de entrada: cadena, fader, ruteo y envíos
        for (int i = 0; i < patch->numInputs; ++i)
        {
            auto& s = *patch->strips[(size_t) i];
            s.work.copyFrom (0, 0, s.raw, 0, 0, n);
            s.work.copyFrom (1, 0, s.raw, 1, 0, n);
            runStrip (s, n, anySolo, patch->sources);
            for (auto& [pos, lvl] : s.sends)
            {
                const float g = lvl->load (std::memory_order_relaxed);
                if (g <= 0.0f || pos < 0 || pos >= (int) patch->strips.size()) continue;
                auto& bus = *patch->strips[(size_t) pos];
                bus.raw.addFrom (0, 0, s.work, 0, 0, n, g);
                bus.raw.addFrom (1, 0, s.work, 1, 0, n, g);
            }
        }
        // 3) buses
        for (size_t i = (size_t) patch->numInputs; i < patch->strips.size(); ++i)
        {
            auto& s = *patch->strips[i];
            s.work.copyFrom (0, 0, s.raw, 0, 0, n);
            s.work.copyFrom (1, 0, s.raw, 1, 0, n);
            runStrip (s, n, anySolo, patch->sources);
        }

        // 4) salidas
        finishOutput (monitorOut, monitorBuf, n, monitorLimiter);
        finishOutput (streamOut, streamBuf, n, streamLimiter);
        if (numOut > 0 && out[0]) juce::FloatVectorOperations::copy (out[0], monitorBuf.getReadPointer (0), n);
        if (numOut > 1 && out[1]) juce::FloatVectorOperations::copy (out[1], monitorBuf.getReadPointer (1), n);
        if (streamBridge != nullptr)
        {
            const float* st[2] { streamBuf.getReadPointer (0), streamBuf.getReadPointer (1) };
            streamBridge->push (st, 2, n);
        }
    }

    void prepareOutputs (double sr, int block)
    {
        juce::dsp::ProcessSpec spec { sr, (juce::uint32) std::max (block, 64), 2 };
        monitorLimiter.prepare (spec); streamLimiter.prepare (spec);
        monitorLimiter.setThreshold (-0.8f); monitorLimiter.setRelease (80.0f);
        streamLimiter.setThreshold (-0.8f); streamLimiter.setRelease (80.0f);
    }

    /** Patch actual (solo para leerla desde el hilo de mensajes con el audio detenido). */
    Patch* currentPatchUnsafe() const { return current; }

    OutputState monitorOut, streamOut;
    std::atomic<bool> running { false };

private:
    static void meterMax (std::atomic<float>& m, float v)
    {
        float cur = m.load (std::memory_order_relaxed);
        if (v > cur) m.store (v, std::memory_order_relaxed);
    }

    void runStrip (PatchStrip& s, int n, bool anySolo, const SourceBuffers& src)
    {
        auto* L = s.work.getWritePointer (0);
        auto* R = s.work.getWritePointer (1);
        for (auto& slot : s.chain)
        {
            if (! slot->enabled.load (std::memory_order_relaxed) || slot->failed.load()) continue;
            if (slot->fx) slot->fx->process (L, R, n, src);
            else if (slot->plugin) slot->plugin->process (L, R, n);
        }
        // Protección: si un efecto devuelve NaN/inf, se apaga el canal en este bloque
        if (! std::isfinite (L[0]) || ! std::isfinite (R[n - 1]))
        {
            s.work.clear (0, n);
        }

        auto& st = *s.st;
        const bool muted = st.mute.load() || (anySolo && ! st.solo.load());
        st.gainSmooth.setTargetValue (muted ? 0.0f : st.gain.load());
        const float pan = st.pan.load();
        const float a = (pan + 1.0f) * juce::MathConstants<float>::pi * 0.25f;
        const float gl = std::abs (pan) < 0.001f ? 1.0f : std::cos (a) * juce::MathConstants<float>::sqrt2;
        const float gr = std::abs (pan) < 0.001f ? 1.0f : std::sin (a) * juce::MathConstants<float>::sqrt2;
        float pk = 0;
        for (int i = 0; i < n; ++i)
        {
            const float g = st.gainSmooth.getNextValue();
            L[i] *= g * gl; R[i] *= g * gr;
            pk = std::max (pk, std::max (std::abs (L[i]), std::abs (R[i])));
        }
        meterMax (st.peak, pk);
        if (st.toMonitor.load())
        {
            monitorBuf.addFrom (0, 0, L, n); monitorBuf.addFrom (1, 0, R, n);
        }
        if (st.toStream.load())
        {
            streamBuf.addFrom (0, 0, L, n); streamBuf.addFrom (1, 0, R, n);
        }
    }

    void finishOutput (OutputState& o, juce::AudioBuffer<float>& b, int n, juce::dsp::Limiter<float>& lim)
    {
        const float g = o.mute.load() ? 0.0f : o.gain.load();
        b.applyGain (0, n, g);
        const float pre = b.getMagnitude (0, n);
        meterMax (o.prePeak, pre);
        if (pre > 0.9f) o.limitHits.fetch_add (1);
        if (o.limiter.load())
        {
            float* c[2] { b.getWritePointer (0), b.getWritePointer (1) };
            juce::dsp::AudioBlock<float> blk (c, 2, (size_t) n);
            lim.process (juce::dsp::ProcessContextReplacing<float> (blk));
        }
        for (int ch = 0; ch < 2; ++ch)
            juce::FloatVectorOperations::clip (b.getWritePointer (ch), b.getReadPointer (ch), -1.0f, 1.0f, n);
        meterMax (o.peak, b.getMagnitude (0, n));
    }

    void retire (Patch* p)
    {
        if (p == nullptr) return;
        for (auto& r : retired)
        {
            Patch* expected = nullptr;
            if (r.compare_exchange_strong (expected, p)) return;
        }
        // cola llena (no debería pasar): se filtra la memoria antes que bloquear el audio
    }

    Patch* current = nullptr;                 // solo hilo de audio (o mensajes si no hay audio)
    std::atomic<Patch*> pending { nullptr };
    std::array<std::atomic<Patch*>, 64> retired;
    juce::AudioBuffer<float> monitorBuf, streamBuf, pcBuf;
    juce::dsp::Limiter<float> monitorLimiter, streamLimiter;
};
} // namespace vc
