#pragma once

#include "JuceConfig.h"
#include <juce_audio_devices/juce_audio_devices.h>
#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_basics/juce_gui_basics.h>

#include "ClockBridge.h"
#include "Mixer.h"

#include <functional>
#include <map>

namespace vc
{
struct AudioSettings
{
    juce::String driverType { "ASIO" };
    juce::String device;              // dispositivo principal (entrada y salida)
    double sampleRate = 48000.0;
    int bufferSize = 64;
    int monitorLeft = 0;              // primer canal de salida del monitor (par L/R)
    juce::String pcDevice { "CABLE Output (VB-Audio Virtual Cable)" };   // captura WASAPI del PC
    juce::String streamDevice;        // salida WASAPI para OBS/Discord (vacío = ninguna)
    int pcBufferMs = 10;              // colchón del puente VB-Cable → interfaz
};

struct OutputModel
{
    float gainDb = 0.0f;
    bool mute = false, limiter = true;
};

struct StripModel
{
    juce::String id, name, kind { "input" }, source;   // kind: input | bus ; source: mic | pc | ""
    juce::String inputMode { "asio" };                 // asio | capture | none
    juce::Array<int> channels { 0 };
    bool stereo = false;
    float gainDb = 0.0f, pan = 0.0f;
    bool mute = false, solo = false, toMonitor = true, toStream = true;
    std::map<juce::String, float> sends;
    std::vector<SlotPtr> chain;
    StripStatePtr st = std::make_shared<StripState>();
};

class PluginWindow;

class Engine : public juce::AudioIODeviceCallback,
               private juce::Timer,
               private juce::ChangeListener
{
public:
    Engine();
    ~Engine() override;

    /** Procesa un comando JSON (hilo de mensajes) y devuelve la respuesta. */
    juce::var handle (const juce::var& msg);

    void loadSession();
    void saveSession();
    void markDirty() { dirtyAt = juce::Time::getMillisecondCounter(); }

    std::function<void (const juce::String&)> sendEvent;   // lo conecta el servidor
    std::function<void()> requestQuit;

    // AudioIODeviceCallback (dispositivo principal)
    void audioDeviceIOCallbackWithContext (const float* const* in, int numIn, float* const* out, int numOut, int n,
                                           const juce::AudioIODeviceCallbackContext&) override;
    void audioDeviceAboutToStart (juce::AudioIODevice*) override;
    void audioDeviceStopped() override;
    void audioDeviceError (const juce::String& msg) override;

    static juce::File configDir();
    void log (const juce::String& s);

private:
    // dispositivos
    juce::String openAudio();
    void openSecondary();
    void closeSecondary();
    juce::var describeDevices();

    // sesión
    void createDefaultSession();
    void loadStrips (const juce::var& v);
    void rebuildPatch();
    void prepareAll();
    void prepareSlot (Slot& s);
    StripModel* findStrip (const juce::String& id);
    SlotPtr findSlot (StripModel& s, const juce::String& uid, int* index = nullptr);
    juce::var stateJson (bool withPluginState);
    juce::var stripJson (const StripModel& s, bool withPluginState);
    juce::var slotJson (const Slot& s, bool withPluginState);
    void applyStripJson (StripModel& s, const juce::var& v);
    SlotPtr slotFromJson (const juce::var& v);
    SlotPtr createBuiltinSlot (const juce::String& type);
    SlotPtr createPluginSlot (const juce::String& path, const juce::String& name, juce::String& error);
    void applyStripStateToAtomics (StripModel& s);
    juce::String newId (const char* prefix);

    // plugins
    juce::var pluginParams (Slot& s);
    void openEditor (const juce::String& stripId, SlotPtr slot);
    void closeEditor (const juce::String& uid);

    void timerCallback() override;
    void changeListenerCallback (juce::ChangeBroadcaster*) override;
    void sendMeters();

    juce::AudioDeviceManager dm;
    juce::AudioPluginFormatManager formats;
    Mixer mixer;
    ClockBridge pcBridge, streamBridge;
    SimplePlayHead playHead;

    std::unique_ptr<juce::AudioIODeviceType> wasapi;
    std::unique_ptr<juce::AudioIODevice> pcDevice, streamDevice;
    struct PcCallback;
    struct StreamCallback;
    std::unique_ptr<PcCallback> pcCb;
    std::unique_ptr<StreamCallback> streamCb;
    std::atomic<bool> pcActive { false }, streamActive { false };
    double pcRate = 48000.0, streamRate = 48000.0;
    juce::String pcError, streamError, audioError;

    AudioSettings settings;
    OutputModel outMonitor, outStream;
    std::vector<StripModel> strips;
    std::map<juce::String, std::unique_ptr<PluginWindow>> windows;

    double sr = 48000.0;
    int block = 128;
    std::atomic<int> lateBlocks { 0 };
    std::atomic<double> maxProcMs { 0.0 };
    juce::uint32 dirtyAt = 0;
    int idCounter = 0;
    int meterTick = 0;
    std::unique_ptr<juce::FileLogger> logger;
};
} // namespace vc
