// Efectos nativos del motor. Todo lo que corre en el hilo de audio es libre de
// reservas de memoria y de bloqueos: los parámetros llegan por atómicos.
//
// Para agregar un efecto: crear una clase que herede de BuiltinFx, darle specs()
// y process(), y registrarla en createBuiltin()/builtinTypes() al final del archivo.
#pragma once

#include "JuceConfig.h"
#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_dsp/juce_dsp.h>

#include <array>
#include <atomic>
#include <cmath>
#include <memory>
#include <vector>

namespace vc
{
constexpr int kMaxParams = 16;
constexpr int kMaxBlock = 8192;

inline float dbToLin (float db) { return db <= -119.0f ? 0.0f : std::pow (10.0f, db / 20.0f); }
inline float linToDb (float x) { return 20.0f * std::log10 (std::max (x, 1.0e-9f)); }

struct ParamSpec
{
    const char* id;
    const char* label;
    const char* kind;      // float | choice | bool | source
    float min, max, def;
    const char* unit;
    bool log = false;
    int decimals = 1;
    std::vector<const char*> choices {};
};

/** Señales crudas de los canales de entrada (para sidechain). */
struct SourceBuffers
{
    std::array<const float*, 8> left {}, right {};
    int count = 0;
};

class BuiltinFx
{
public:
    virtual ~BuiltinFx() = default;
    virtual const char* type() const = 0;
    virtual const char* label() const = 0;
    virtual const char* category() const = 0;
    virtual const std::vector<ParamSpec>& specs() const = 0;

    virtual void prepare (double sampleRate, int maxBlock) = 0;
    virtual void reset() {}
    virtual void process (float* L, float* R, int n, const SourceBuffers& src) = 0;

    void initDefaults()
    {
        const auto& s = specs();
        for (size_t i = 0; i < s.size(); ++i)
            values[i].store (s[i].def);
    }

    int indexOf (const juce::String& pid) const
    {
        const auto& s = specs();
        for (size_t i = 0; i < s.size(); ++i)
            if (pid == s[i].id) return (int) i;
        return -1;
    }

    float get (int i) const { return values[(size_t) i].load (std::memory_order_relaxed); }

    std::array<std::atomic<float>, kMaxParams> values {};
    std::atomic<float> meter { 0.0f };        // dato para mostrar (ej. reducción del ducker)
    std::atomic<int> sourceIndex { -1 };       // solo ducker: índice del canal de sidechain
    juce::String sourceName { "Mic" };         // solo ducker (hilo de mensajes)

protected:
    double sr = 48000.0;
};

// ---------------------------------------------------------------------------
// Biquad (fórmulas RBJ). Se recalcula solo cuando cambia un parámetro.
// ---------------------------------------------------------------------------
struct Biquad
{
    double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0;
    double z1[2] {}, z2[2] {};

    void reset() { z1[0] = z1[1] = z2[0] = z2[1] = 0; }

    void set (double nb0, double nb1, double nb2, double na0, double na1, double na2)
    {
        b0 = nb0 / na0; b1 = nb1 / na0; b2 = nb2 / na0; a1 = na1 / na0; a2 = na2 / na0;
    }

    void highpass (double sr, double f, double q)
    {
        const double w = 2 * juce::MathConstants<double>::pi * f / sr, c = std::cos (w), a = std::sin (w) / (2 * q);
        set ((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + a, -2 * c, 1 - a);
    }
    void lowpass (double sr, double f, double q)
    {
        const double w = 2 * juce::MathConstants<double>::pi * f / sr, c = std::cos (w), a = std::sin (w) / (2 * q);
        set ((1 - c) / 2, 1 - c, (1 - c) / 2, 1 + a, -2 * c, 1 - a);
    }
    void peak (double sr, double f, double q, double db)
    {
        const double A = std::pow (10.0, db / 40), w = 2 * juce::MathConstants<double>::pi * f / sr;
        const double c = std::cos (w), a = std::sin (w) / (2 * q);
        set (1 + a * A, -2 * c, 1 - a * A, 1 + a / A, -2 * c, 1 - a / A);
    }
    void lowShelf (double sr, double f, double db)
    {
        const double A = std::pow (10.0, db / 40), w = 2 * juce::MathConstants<double>::pi * f / sr;
        const double c = std::cos (w), s = std::sin (w), a = s / 2 * std::sqrt (2.0), sq = 2 * std::sqrt (A) * a;
        set (A * ((A + 1) - (A - 1) * c + sq), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - sq),
             (A + 1) + (A - 1) * c + sq, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sq);
    }
    void highShelf (double sr, double f, double db)
    {
        const double A = std::pow (10.0, db / 40), w = 2 * juce::MathConstants<double>::pi * f / sr;
        const double c = std::cos (w), s = std::sin (w), a = s / 2 * std::sqrt (2.0), sq = 2 * std::sqrt (A) * a;
        set (A * ((A + 1) + (A - 1) * c + sq), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sq),
             (A + 1) - (A - 1) * c + sq, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sq);
    }

    inline float tick (int ch, float x)
    {
        const double y = b0 * x + z1[ch];
        z1[ch] = b1 * x - a1 * y + z2[ch];
        z2[ch] = b2 * x - a2 * y;
        return (float) y;
    }

    void process (float* L, float* R, int n)
    {
        for (int i = 0; i < n; ++i) { L[i] = tick (0, L[i]); R[i] = tick (1, R[i]); }
    }
};

/** Línea de retardo estéreo simple con lectura fraccional. */
struct DelayLine
{
    std::vector<float> l, r;
    int w = 0, size = 1;

    void prepare (int maxSamples)
    {
        size = juce::nextPowerOfTwo (maxSamples + 4);
        l.assign ((size_t) size, 0.0f);
        r.assign ((size_t) size, 0.0f);
        w = 0;
    }
    void reset() { std::fill (l.begin(), l.end(), 0.0f); std::fill (r.begin(), r.end(), 0.0f); }
    void push (float a, float b) { l[(size_t) w] = a; r[(size_t) w] = b; w = (w + 1) & (size - 1); }
    float read (const std::vector<float>& buf, double delay) const
    {
        const double pos = (double) w - 1.0 - delay;
        const int i0 = (int) std::floor (pos);
        const float fr = (float) (pos - i0);
        const float a = buf[(size_t) (i0 & (size - 1))], b = buf[(size_t) ((i0 + 1) & (size - 1))];
        return a + (b - a) * fr;
    }
};

#define VC_SPECS(...) const std::vector<ParamSpec>& specs() const override { static const std::vector<ParamSpec> s { __VA_ARGS__ }; return s; }

// ---------------------------------------------------------------------------
class GainFx : public BuiltinFx
{
public:
    const char* type() const override { return "gain"; }
    const char* label() const override { return "Ganancia"; }
    const char* category() const override { return "Utilidad"; }
    VC_SPECS ({ "gain_db", "Ganancia", "float", -24, 24, 0, "dB" })
    void prepare (double s, int) override { sr = s; g.reset (s, 0.02); g.setCurrentAndTargetValue (dbToLin (get (0))); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        g.setTargetValue (dbToLin (get (0)));
        for (int i = 0; i < n; ++i) { const float k = g.getNextValue(); L[i] *= k; R[i] *= k; }
    }
private:
    juce::LinearSmoothedValue<float> g;
};

// ---------------------------------------------------------------------------
class GateFx : public BuiltinFx
{
public:
    const char* type() const override { return "gate"; }
    const char* label() const override { return "Puerta de ruido"; }
    const char* category() const override { return "Dinámica"; }
    VC_SPECS ({ "threshold_db", "Umbral", "float", -90, 0, -50, "dB" },
              { "ratio", "Ratio", "float", 1, 30, 10, ":1" },
              { "attack_ms", "Ataque", "float", 0.1f, 50, 1, "ms", true },
              { "release_ms", "Release", "float", 5, 1000, 120, "ms", true })
    void prepare (double s, int maxBlock) override { sr = s; gate.prepare ({ s, (juce::uint32) maxBlock, 2 }); }
    void reset() override { gate.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        gate.setThreshold (get (0)); gate.setRatio (get (1)); gate.setAttack (get (2)); gate.setRelease (get (3));
        float* ch[2] { L, R };
        juce::dsp::AudioBlock<float> b (ch, 2, (size_t) n);
        gate.process (juce::dsp::ProcessContextReplacing<float> (b));
    }
private:
    juce::dsp::NoiseGate<float> gate;
};

// ---------------------------------------------------------------------------
class CompressorFx : public BuiltinFx
{
public:
    const char* type() const override { return "compressor"; }
    const char* label() const override { return "Compresor"; }
    const char* category() const override { return "Dinámica"; }
    VC_SPECS ({ "threshold_db", "Umbral", "float", -60, 0, -18, "dB" },
              { "ratio", "Ratio", "float", 1, 20, 3, ":1" },
              { "attack_ms", "Ataque", "float", 0.1f, 200, 5, "ms", true },
              { "release_ms", "Release", "float", 10, 1000, 120, "ms", true },
              { "makeup_db", "Makeup", "float", 0, 24, 4, "dB" })
    void prepare (double s, int maxBlock) override { sr = s; comp.prepare ({ s, (juce::uint32) maxBlock, 2 }); }
    void reset() override { comp.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        comp.setThreshold (get (0)); comp.setRatio (get (1)); comp.setAttack (get (2)); comp.setRelease (get (3));
        float* ch[2] { L, R };
        juce::dsp::AudioBlock<float> b (ch, 2, (size_t) n);
        comp.process (juce::dsp::ProcessContextReplacing<float> (b));
        const float mk = dbToLin (get (4));
        juce::FloatVectorOperations::multiply (L, mk, n);
        juce::FloatVectorOperations::multiply (R, mk, n);
    }
private:
    juce::dsp::Compressor<float> comp;
};

// ---------------------------------------------------------------------------
class EqFx : public BuiltinFx
{
public:
    const char* type() const override { return "eq"; }
    const char* label() const override { return "EQ 4 bandas"; }
    const char* category() const override { return "Tono"; }
    VC_SPECS ({ "hpf", "Pasa altos", "float", 20, 400, 80, "Hz", true, 0 },
              { "low_f", "Graves frec", "float", 60, 500, 180, "Hz", true, 0 },
              { "low_g", "Graves", "float", -15, 15, 0, "dB" },
              { "mid_f", "Medios frec", "float", 300, 6000, 2500, "Hz", true, 0 },
              { "mid_g", "Medios", "float", -15, 15, 0, "dB" },
              { "mid_q", "Medios Q", "float", 0.3f, 6, 1, "" },
              { "high_f", "Agudos frec", "float", 2000, 16000, 9000, "Hz", true, 0 },
              { "high_g", "Agudos", "float", -15, 15, 0, "dB" })
    void prepare (double s, int) override { sr = s; cache.fill (-1e9f); for (auto& b : bq) b.reset(); }
    void reset() override { for (auto& b : bq) b.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        bool dirty = false;
        for (int i = 0; i < 8; ++i) { const float v = get (i); if (v != cache[(size_t) i]) { cache[(size_t) i] = v; dirty = true; } }
        if (dirty)
        {
            const double nyq = sr * 0.45;
            bq[0].highpass (sr, std::min ((double) cache[0], nyq), 0.707);
            bq[1].lowShelf (sr, std::min ((double) cache[1], nyq), cache[2]);
            bq[2].peak (sr, std::min ((double) cache[3], nyq), cache[5], cache[4]);
            bq[3].highShelf (sr, std::min ((double) cache[6], nyq), cache[7]);
        }
        bq[0].process (L, R, n);
        if (std::abs (cache[2]) > 0.01f) bq[1].process (L, R, n);
        if (std::abs (cache[4]) > 0.01f) bq[2].process (L, R, n);
        if (std::abs (cache[7]) > 0.01f) bq[3].process (L, R, n);
    }
private:
    std::array<Biquad, 4> bq;
    std::array<float, 8> cache {};
};

// ---------------------------------------------------------------------------
class SaturationFx : public BuiltinFx
{
public:
    const char* type() const override { return "saturation"; }
    const char* label() const override { return "Saturación"; }
    const char* category() const override { return "Color"; }
    VC_SPECS ({ "drive_db", "Drive", "float", 0, 40, 8, "dB" },
              { "mix", "Mezcla", "float", 0, 1, 0.3f, "", false, 2 })
    void prepare (double s, int) override { sr = s; }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        const float d = dbToLin (get (0)), comp = 1.0f / std::tanh (d), m = get (1);
        for (int i = 0; i < n; ++i)
        {
            L[i] = L[i] * (1 - m) + std::tanh (L[i] * d) * comp * m * 0.7f;
            R[i] = R[i] * (1 - m) + std::tanh (R[i] * d) * comp * m * 0.7f;
        }
    }
};

// ---------------------------------------------------------------------------
class ChorusFx : public BuiltinFx
{
public:
    const char* type() const override { return "chorus"; }
    const char* label() const override { return "Chorus"; }
    const char* category() const override { return "Modulación"; }
    VC_SPECS ({ "rate_hz", "Velocidad", "float", 0.05f, 5, 0.8f, "Hz", true, 2 },
              { "depth", "Profundidad", "float", 0, 1, 0.25f, "", false, 2 },
              { "centre_delay_ms", "Retardo", "float", 1, 30, 7, "ms" },
              { "feedback", "Feedback", "float", 0, 0.9f, 0, "", false, 2 },
              { "mix", "Mezcla", "float", 0, 1, 0.35f, "", false, 2 })
    void prepare (double s, int maxBlock) override { sr = s; ch.prepare ({ s, (juce::uint32) maxBlock, 2 }); }
    void reset() override { ch.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        ch.setRate (get (0)); ch.setDepth (get (1)); ch.setCentreDelay (get (2)); ch.setFeedback (get (3)); ch.setMix (get (4));
        float* c[2] { L, R };
        juce::dsp::AudioBlock<float> b (c, 2, (size_t) n);
        ch.process (juce::dsp::ProcessContextReplacing<float> (b));
    }
private:
    juce::dsp::Chorus<float> ch;
};

// ---------------------------------------------------------------------------
class ReverbFx : public BuiltinFx
{
public:
    const char* type() const override { return "reverb"; }
    const char* label() const override { return "Reverb"; }
    const char* category() const override { return "Espacio"; }
    VC_SPECS ({ "room_size", "Tamaño", "float", 0, 1, 0.6f, "", false, 2 },
              { "damping", "Amortiguación", "float", 0, 1, 0.5f, "", false, 2 },
              { "width", "Ancho", "float", 0, 1, 1, "", false, 2 },
              { "wet_level", "Húmedo", "float", 0, 1, 1, "", false, 2 },
              { "dry_level", "Seco", "float", 0, 1, 0, "", false, 2 },
              { "predelay_ms", "Pre-delay", "float", 0, 150, 20, "ms" })
    void prepare (double s, int) override { sr = s; rv.setSampleRate (s); pre.prepare ((int) (s * 0.2)); }
    void reset() override { rv.reset(); pre.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        juce::Reverb::Parameters p;
        p.roomSize = get (0); p.damping = get (1); p.width = get (2);
        p.wetLevel = get (3); p.dryLevel = 0.0f; p.freezeMode = 0.0f;
        rv.setParameters (p);
        const float dry = get (4);
        const double d = get (5) * sr / 1000.0;
        n = std::min (n, kMaxBlock);
        for (int i = 0; i < n; ++i)
        {
            const float l = L[i], r = R[i];
            pre.push (l, r);
            dryL[(size_t) i] = l * dry; dryR[(size_t) i] = r * dry;
            if (d >= 1) { L[i] = pre.read (pre.l, d); R[i] = pre.read (pre.r, d); }
        }
        rv.processStereo (L, R, n);
        for (int i = 0; i < n; ++i) { L[i] += dryL[(size_t) i]; R[i] += dryR[(size_t) i]; }
    }
private:
    juce::Reverb rv;
    DelayLine pre;
    std::array<float, kMaxBlock> dryL {}, dryR {};
};

// ---------------------------------------------------------------------------
class DelayFx : public BuiltinFx
{
public:
    const char* type() const override { return "delay"; }
    const char* label() const override { return "Delay"; }
    const char* category() const override { return "Espacio"; }
    VC_SPECS ({ "time_ms", "Tiempo", "float", 10, 2000, 375, "ms", true, 0 },
              { "feedback", "Repeticiones", "float", 0, 0.95f, 0.35f, "", false, 2 },
              { "mix", "Mezcla", "float", 0, 1, 1, "", false, 2 },
              { "hpf", "Corte graves", "float", 20, 1000, 200, "Hz", true, 0 },
              { "lpf", "Corte agudos", "float", 1000, 20000, 6000, "Hz", true, 0 },
              { "pingpong", "Ping-pong", "bool", 0, 1, 0, "" })
    void prepare (double s, int) override
    {
        sr = s; line.prepare ((int) (s * 2.1)); hp.reset(); lp.reset();
        time.reset (s, 0.08); time.setCurrentAndTargetValue (get (0) * (float) s / 1000.0f);
    }
    void reset() override { line.reset(); hp.reset(); lp.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        if (get (3) != cHp) { cHp = get (3); hp.highpass (sr, cHp, 0.707); }
        if (get (4) != cLp) { cLp = get (4); lp.lowpass (sr, std::min ((double) cLp, sr * 0.45), 0.707); }
        time.setTargetValue (juce::jlimit (1.0f, (float) (sr * 2.0), get (0) * (float) sr / 1000.0f));
        const float fb = get (1), mix = get (2);
        const bool pp = get (5) > 0.5f;
        for (int i = 0; i < n; ++i)
        {
            const double d = time.getNextValue();
            float wl = line.read (line.l, d), wr = line.read (line.r, d);
            float fl = lp.tick (0, hp.tick (0, wl)), fr = lp.tick (1, hp.tick (1, wr));
            if (pp) std::swap (fl, fr);
            const float inL = L[i], inR = R[i];
            line.push (pp ? (inL + inR) * 0.5f + fl * fb : inL + fl * fb, pp ? fr * fb : inR + fr * fb);
            L[i] = inL * (1 - mix) + wl * mix;
            R[i] = inR * (1 - mix) + wr * mix;
        }
    }
private:
    DelayLine line;
    Biquad hp, lp;
    float cHp = -1, cLp = -1;
    juce::SmoothedValue<double> time;
};

// ---------------------------------------------------------------------------
class LimiterFx : public BuiltinFx
{
public:
    const char* type() const override { return "limiter"; }
    const char* label() const override { return "Limitador"; }
    const char* category() const override { return "Dinámica"; }
    VC_SPECS ({ "threshold_db", "Techo", "float", -24, 0, -1, "dB" },
              { "release_ms", "Release", "float", 10, 1000, 100, "ms", true })
    void prepare (double s, int maxBlock) override { sr = s; lim.prepare ({ s, (juce::uint32) maxBlock, 2 }); }
    void reset() override { lim.reset(); }
    void process (float* L, float* R, int n, const SourceBuffers&) override
    {
        lim.setThreshold (get (0)); lim.setRelease (get (1));
        float* c[2] { L, R };
        juce::dsp::AudioBlock<float> b (c, 2, (size_t) n);
        lim.process (juce::dsp::ProcessContextReplacing<float> (b));
    }
private:
    juce::dsp::Limiter<float> lim;
};

// ---------------------------------------------------------------------------
/** Baja el volumen de este canal cuando suena la fuente elegida (sidechain). */
class DuckerFx : public BuiltinFx
{
public:
    const char* type() const override { return "ducker"; }
    const char* label() const override { return "Sidechain (ducker)"; }
    const char* category() const override { return "Dinámica"; }
    VC_SPECS ({ "source", "Fuente", "source", 0, 0, 0, "" },
              { "threshold_db", "Umbral", "float", -70, 0, -40, "dB" },
              { "depth_db", "Reducción", "float", 0, 40, 10, "dB" },
              { "attack_ms", "Ataque", "float", 1, 300, 15, "ms", true },
              { "release_ms", "Release", "float", 20, 3000, 300, "ms", true })
    void prepare (double s, int) override { sr = s; env = 0; red = 0; }
    void reset() override { env = 0; red = 0; }
    void process (float* L, float* R, int n, const SourceBuffers& src) override
    {
        const int si = sourceIndex.load();
        if (si < 0 || si >= src.count || src.left[(size_t) si] == nullptr) { meter.store (0); return; }
        const float* kl = src.left[(size_t) si];
        const float* kr = src.right[(size_t) si];
        const float thr = get (1), depth = get (2);
        const float aA = std::exp (-1.0f / (0.001f * get (3) * (float) sr));
        const float aR = std::exp (-1.0f / (0.001f * get (4) * (float) sr));
        const float aEnv = std::exp (-1.0f / (0.005f * (float) sr));
        for (int i = 0; i < n; ++i)
        {
            const float x = std::max (std::abs (kl[i]), std::abs (kr[i]));
            env = x > env ? x : env * aEnv + x * (1 - aEnv);
            const float over = linToDb (env) - thr;
            const float target = depth * juce::jlimit (0.0f, 1.0f, over / 6.0f);
            const float a = target > red ? aA : aR;
            red = target + (red - target) * a;
            const float g = dbToLin (-red);
            L[i] *= g; R[i] *= g;
        }
        meter.store (red);
    }
private:
    float env = 0, red = 0;
};

#undef VC_SPECS

inline std::unique_ptr<BuiltinFx> createBuiltin (const juce::String& t)
{
    std::unique_ptr<BuiltinFx> fx;
    if (t == "gain") fx = std::make_unique<GainFx>();
    else if (t == "gate") fx = std::make_unique<GateFx>();
    else if (t == "compressor") fx = std::make_unique<CompressorFx>();
    else if (t == "eq") fx = std::make_unique<EqFx>();
    else if (t == "saturation") fx = std::make_unique<SaturationFx>();
    else if (t == "chorus") fx = std::make_unique<ChorusFx>();
    else if (t == "reverb") fx = std::make_unique<ReverbFx>();
    else if (t == "delay") fx = std::make_unique<DelayFx>();
    else if (t == "limiter") fx = std::make_unique<LimiterFx>();
    else if (t == "ducker") fx = std::make_unique<DuckerFx>();
    if (fx) fx->initDefaults();
    return fx;
}

inline juce::StringArray builtinTypes()
{
    return { "gate", "eq", "compressor", "limiter", "ducker", "saturation", "chorus", "reverb", "delay", "gain" };
}
} // namespace vc
