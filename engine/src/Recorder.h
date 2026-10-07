// Recorder — graba la salida Monitor (lo que escucha Chacho) sin bloquear el hilo de audio.
//
//   hilo ASIO      : pushBlock(L, R, n)      -> copia a un FIFO lock-free (juce::AbstractFifo), sin locks
//                                               ni reservas de memoria. Si el FIFO se llena cuenta "dropped".
//   hilo escritor  : vacía el FIFO cada ~5 ms a un WAV float 32 (juce::WavAudioFormat) y hace flush()
//                    cada 2 s (el WAV queda válido aunque la app se caiga).
//   hilo comandos  : start(path) / marker(name) / stop()  (rec_start, rec_marker, rec_stop)
//
// La conversión a MP3 la hace la interfaz (Python + lameenc) cuando llega la respuesta de rec_stop.
#pragma once

#include "JuceConfig.h"
#include <juce_audio_formats/juce_audio_formats.h>
#include <atomic>
#include <memory>
#include <mutex>
#include <vector>

class Recorder : private juce::Thread
{
public:
    struct Marker { double seconds; juce::String name; };
    struct Result {
        bool ok = false;
        double seconds = 0.0;
        juce::int64 frames = 0;
        juce::int64 dropped = 0;      // muestras perdidas por FIFO lleno (debería ser 0)
        std::vector<Marker> markers;
        juce::String path, error;
    };

    Recorder();
    ~Recorder() override;

    // Llamar al (re)abrir el dispositivo, NO desde el hilo de audio. fifoSeconds = colchón ante
    // pausas del disco (4 s a 48 kHz = 1.5 MB).
    void prepare (double sampleRate, double fifoSeconds = 4.0);

    // --- hilo de comandos
    bool start (const juce::File& wavFile, juce::String& error);
    double marker (const juce::String& name);          // devuelve el tiempo (s) de la marca
    Result stop();
    bool isRecording() const noexcept { return armed.load (std::memory_order_acquire); }
    double elapsedSeconds() const noexcept;
    juce::int64 droppedSamples() const noexcept { return dropped.load (std::memory_order_relaxed); }

    // --- hilo de audio (RT-safe). right puede ser nullptr (mono -> se duplica).
    void pushBlock (const float* left, const float* right, int numSamples) noexcept;

private:
    void run() override;
    void drain (bool all);

    juce::AbstractFifo fifo { 1 };
    std::vector<float> bufL, bufR;
    double sr = 48000.0;

    std::atomic<bool> armed { false };
    std::atomic<bool> pushing { false };
    std::atomic<juce::int64> framesPushed { 0 }, dropped { 0 };

    std::unique_ptr<juce::AudioFormatWriter> writer;
    juce::int64 framesWritten = 0;
    juce::uint32 lastFlushMs = 0;
    bool writeError = false;

    std::mutex markerLock;               // solo hilos de comandos / escritor, nunca el de audio
    std::vector<Marker> markers;
    juce::File currentFile;

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR (Recorder)
};
