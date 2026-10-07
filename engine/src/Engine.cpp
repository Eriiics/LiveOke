#include "Engine.h"
#include "PluginWindowStyle.h"

namespace vc
{
using juce::String;
using juce::var;

// ---------------------------------------------------------------------------
// utilidades JSON
// ---------------------------------------------------------------------------
namespace
{
struct Obj
{
    juce::DynamicObject::Ptr o { new juce::DynamicObject() };
    Obj& set (const char* k, const var& v) { o->setProperty (k, v); return *this; }
    operator var() const { return var (o.get()); }
};

var ok (const var& data = {})
{
    Obj r; r.set ("ok", true);
    if (! data.isVoid()) r.set ("data", data);
    return r;
}

var fail (const String& e)
{
    return Obj().set ("ok", false).set ("error", e);
}

bool has (const var& v, const char* k) { return v.hasProperty (k); }

var intArray (const juce::Array<int>& a)
{
    juce::Array<var> r;
    for (int x : a) r.add (x);
    return r;
}

float toDbGain (float db) { return db <= -100.0f ? 0.0f : std::pow (10.0f, db / 20.0f); }
} // namespace

// ---------------------------------------------------------------------------
class PluginWindow : public juce::DocumentWindow
{
public:
    PluginWindow (const String& title, juce::AudioProcessorEditor* ed, std::function<void()> onClose)
        : juce::DocumentWindow (title, juce::Colour (0xff181a1f),
                                juce::DocumentWindow::closeButton | juce::DocumentWindow::minimiseButton),
          closeFn (std::move (onClose))
    {
        setUsingNativeTitleBar (true);
        setContentOwned (ed, true);
        setResizable (ed->isResizable(), false);
        centreWithSize (getWidth(), getHeight());
        setVisible (true);
        toFront (true);
    }
    void closeButtonPressed() override { if (closeFn) closeFn(); }

private:
    std::function<void()> closeFn;
};

struct Engine::PcCallback : public juce::AudioIODeviceCallback
{
    explicit PcCallback (Engine& en) : e (en) {}
    void audioDeviceIOCallbackWithContext (const float* const* in, int numIn, float* const* out, int numOut, int n,
                                           const juce::AudioIODeviceCallbackContext&) override
    {
        for (int c = 0; c < numOut; ++c)
            if (out[c]) juce::FloatVectorOperations::clear (out[c], n);
        e.pcBridge.push (in, numIn, n);
    }
    void audioDeviceAboutToStart (juce::AudioIODevice*) override {}
    void audioDeviceStopped() override {}
    Engine& e;
};

struct Engine::StreamCallback : public juce::AudioIODeviceCallback
{
    explicit StreamCallback (Engine& en) : e (en) {}
    void audioDeviceIOCallbackWithContext (const float* const*, int, float* const* out, int numOut, int n,
                                           const juce::AudioIODeviceCallbackContext&) override
    {
        if (numOut <= 0) return;
        float* L = out[0];
        float* R = numOut > 1 ? out[1] : tmp.getWritePointer (0);
        if (n > tmp.getNumSamples()) { for (int c = 0; c < numOut; ++c) juce::FloatVectorOperations::clear (out[c], n); return; }
        e.streamBridge.pull (L, R, n);
        for (int c = 2; c < numOut; ++c) juce::FloatVectorOperations::clear (out[c], n);
    }
    void audioDeviceAboutToStart (juce::AudioIODevice*) override {}
    void audioDeviceStopped() override {}
    Engine& e;
    juce::AudioBuffer<float> tmp { 1, kMaxBlock };
};

// ---------------------------------------------------------------------------
juce::File Engine::configDir()
{
    auto d = juce::File::getSpecialLocation (juce::File::userHomeDirectory).getChildFile (".vocalchain");
    d.createDirectory();
    return d;
}

void Engine::log (const String& s)
{
    if (logger) logger->logMessage (juce::Time::getCurrentTime().toString (false, true, true, true) + "  " + s);
}

Engine::Engine()
{
    logger = std::make_unique<juce::FileLogger> (configDir().getChildFile ("engine.log"), "VocalEngine", 256 * 1024);
    log ("Inicio del motor");
    formats.addDefaultFormats();
    pcCb = std::make_unique<PcCallback> (*this);
    streamCb = std::make_unique<StreamCallback> (*this);
    dm.addChangeListener (this);
    loadSession();
    for (auto [model, st] : { std::pair<OutputModel*, OutputState*> { &outMonitor, &mixer.monitorOut },
                              std::pair<OutputModel*, OutputState*> { &outStream, &mixer.streamOut } })
    {
        st->gain.store (toDbGain (model->gainDb));
        st->mute.store (model->mute);
        st->limiter.store (model->limiter);
    }
    mixer.startWorkers (3);
    mixer.multithread.store (settings.multithread);
    audioError = openAudio();
    if (audioError.isNotEmpty()) log ("Audio: " + audioError);
    openSecondary();
    rebuildPatch();
    startTimerHz (30);
}

Engine::~Engine()
{
    stopTimer();
    if (recorder.isRecording()) recorder.stop();
    saveSession();
    windows.clear();
    closeSecondary();
    dm.removeAudioCallback (this);
    dm.closeAudioDevice();
    strips.clear();
}

// ---------------------------------------------------------------------------
// Dispositivos
// ---------------------------------------------------------------------------
String Engine::openAudio()
{
    dm.removeAudioCallback (this);
    dm.closeAudioDevice();
    mixer.running.store (false);

    const auto& types = dm.getAvailableDeviceTypes();
    juce::AudioIODeviceType* type = nullptr;
    for (auto* t : types)
        if (t->getTypeName() == settings.driverType) type = t;
    if (type == nullptr)
        for (auto* t : types)
            if (t->getTypeName() == "ASIO") type = t;
    if (type == nullptr && types.size() > 0) type = types[0];
    if (type == nullptr) return "No hay tipos de audio disponibles";

    dm.setCurrentAudioDeviceType (type->getTypeName(), true);
    type->scanForDevices();
    settings.driverType = type->getTypeName();
    auto outs = type->getDeviceNames (false);
    auto ins = type->getDeviceNames (true);

    String dev = settings.device;
    if (dev.isEmpty() || (! outs.contains (dev) && ! ins.contains (dev)))
    {
        dev = {};
        for (auto& n : outs)
            if (n.containsIgnoreCase ("focusrite")) { dev = n; break; }
        if (dev.isEmpty())
            for (auto& n : outs)
                if (! n.containsIgnoreCase ("asio4all") && ! n.containsIgnoreCase ("realtek")) { dev = n; break; }
        if (dev.isEmpty() && outs.size() > 0) dev = outs[0];
    }
    if (dev.isEmpty()) return juce::String::fromUTF8 ("No encontré dispositivos ") + type->getTypeName();
    settings.device = dev;

    juce::AudioDeviceManager::AudioDeviceSetup setup;
    dm.getAudioDeviceSetup (setup);
    setup.outputDeviceName = outs.contains (dev) ? dev : String();
    setup.inputDeviceName = ins.contains (dev) ? dev : (ins.size() > 0 ? ins[0] : String());
    setup.sampleRate = settings.sampleRate;
    setup.bufferSize = settings.bufferSize;
    setup.useDefaultInputChannels = false;
    setup.useDefaultOutputChannels = false;
    setup.inputChannels.clear();
    setup.inputChannels.setRange (0, 16, true);
    setup.outputChannels.clear();
    setup.outputChannels.setRange (settings.monitorLeft, 2, true);

    auto err = dm.setAudioDeviceSetup (setup, true);
    if (err.isNotEmpty())
    {
        log ("setAudioDeviceSetup: " + err);
        // reintento con valores del driver
        setup.bufferSize = 0;
        setup.sampleRate = 0;
        err = dm.setAudioDeviceSetup (setup, true);
    }
    dm.addAudioCallback (this);
    if (auto* d = dm.getCurrentAudioDevice())
        log ("Abierto " + d->getTypeName() + " / " + d->getName() + " @ " + String (d->getCurrentSampleRate())
             + " Hz, buffer " + String (d->getCurrentBufferSizeSamples()));
    return err;
}

void Engine::closeSecondary()
{
    pcActive.store (false);
    streamActive.store (false);
    if (pcDevice) { pcDevice->stop(); pcDevice->close(); pcDevice.reset(); }
    if (streamDevice) { streamDevice->stop(); streamDevice->close(); streamDevice.reset(); }
}

void Engine::openSecondary()
{
    closeSecondary();
    pcError = streamError = {};

    auto openOne = [this] (const String& wanted, bool isInput, std::unique_ptr<juce::AudioIODevice>& devOut,
                           double& rateOut, String& errOut) -> bool
    {
        if (wanted.isEmpty()) return false;
        for (auto mode : { juce::WASAPIDeviceMode::sharedLowLatency, juce::WASAPIDeviceMode::shared })
        {
            std::unique_ptr<juce::AudioIODeviceType> t (juce::AudioIODeviceType::createAudioIODeviceType_WASAPI (mode));
            if (t == nullptr) continue;
            t->scanForDevices();
            auto names = t->getDeviceNames (isInput);
            String name;
            for (auto& n : names)
                if (n == wanted) name = n;
            if (name.isEmpty())
                for (auto& n : names)
                    if (n.startsWithIgnoreCase (wanted.substring (0, 24)) || wanted.startsWithIgnoreCase (n.substring (0, 24))) { name = n; break; }
            if (name.isEmpty()) { errOut = juce::String::fromUTF8 ("No encontré '") + wanted + "'"; return false; }
            std::unique_ptr<juce::AudioIODevice> d (isInput ? t->createDevice ({}, name) : t->createDevice (name, {}));
            if (d == nullptr) continue;
            auto rates = d->getAvailableSampleRates();
            double rate = rates.contains (sr) ? sr : (rates.isEmpty() ? sr : rates[0]);
            juce::BigInteger chans;
            const int nch = std::min (2, (isInput ? d->getInputChannelNames() : d->getOutputChannelNames()).size());
            chans.setRange (0, std::max (nch, 1), true);
            auto err = isInput ? d->open (chans, {}, rate, d->getDefaultBufferSize())
                               : d->open ({}, chans, rate, d->getDefaultBufferSize());
            if (err.isNotEmpty()) { errOut = err; log ("WASAPI " + name + ": " + err); continue; }
            rateOut = d->getCurrentSampleRate();
            log (String ("Secundario ") + (isInput ? "captura " : "salida ") + name + " @ " + String (rateOut)
                 + " Hz, buffer " + String (d->getCurrentBufferSizeSamples())
                 + (mode == juce::WASAPIDeviceMode::sharedLowLatency ? " (baja latencia)" : ""));
            devOut = std::move (d);
            wasapi = std::move (t);
            errOut = {};
            return true;
        }
        return false;
    };

    if (openOne (settings.pcDevice, true, pcDevice, pcRate, pcError))
    {
        const int target = pcDevice->getCurrentBufferSizeSamples() + (int) (settings.pcBufferMs * pcRate / 1000.0);
        pcBridge.configure (pcRate, sr, target);
        pcDevice->start (pcCb.get());
        pcActive.store (true);
    }
    if (openOne (settings.streamDevice, false, streamDevice, streamRate, streamError))
    {
        streamBridge.configure (sr, streamRate, block + (int) (0.01 * sr));
        streamDevice->start (streamCb.get());
        streamActive.store (true);
    }
}

var Engine::describeDevices()
{
    Obj r;
    Obj typesObj;
    for (auto* t : dm.getAvailableDeviceTypes())
    {
        t->scanForDevices();
        juce::Array<var> outs, ins;
        for (auto& n : t->getDeviceNames (false)) outs.add (n);
        for (auto& n : t->getDeviceNames (true)) ins.add (n);
        typesObj.set (t->getTypeName().toRawUTF8(), Obj().set ("outputs", outs).set ("inputs", ins));
    }
    r.set ("types", typesObj);

    // dispositivos WASAPI para PC y Stream
    {
        std::unique_ptr<juce::AudioIODeviceType> t (juce::AudioIODeviceType::createAudioIODeviceType_WASAPI (juce::WASAPIDeviceMode::shared));
        juce::Array<var> caps, outs;
        if (t)
        {
            t->scanForDevices();
            for (auto& n : t->getDeviceNames (true)) caps.add (n);
            for (auto& n : t->getDeviceNames (false)) outs.add (n);
        }
        r.set ("capture_devices", caps).set ("playback_devices", outs);
    }

    Obj cur;
    cur.set ("driver_type", settings.driverType).set ("device", settings.device)
       .set ("pc_device", settings.pcDevice).set ("stream_device", settings.streamDevice)
       .set ("monitor_left", settings.monitorLeft).set ("pc_buffer_ms", settings.pcBufferMs)
       .set ("error", audioError).set ("pc_error", pcError).set ("stream_error", streamError)
       .set ("pc_active", pcActive.load()).set ("stream_active", streamActive.load());
    if (auto* d = dm.getCurrentAudioDevice())
    {
        juce::Array<var> rates, sizes, inNames, outNames;
        for (auto x : d->getAvailableSampleRates()) rates.add (x);
        for (auto x : d->getAvailableBufferSizes()) sizes.add (x);
        for (auto& n : d->getInputChannelNames()) inNames.add (n);
        for (auto& n : d->getOutputChannelNames()) outNames.add (n);
        cur.set ("open", true).set ("name", d->getName()).set ("type", d->getTypeName())
           .set ("sample_rate", d->getCurrentSampleRate()).set ("buffer_size", d->getCurrentBufferSizeSamples())
           .set ("sample_rates", rates).set ("buffer_sizes", sizes)
           .set ("input_channels", inNames).set ("output_channels", outNames)
           .set ("input_latency", d->getInputLatencyInSamples()).set ("output_latency", d->getOutputLatencyInSamples())
           .set ("has_panel", d->hasControlPanel());
    }
    else
        cur.set ("open", false);
    r.set ("current", cur);
    return r;
}

// ---------------------------------------------------------------------------
// Callbacks del dispositivo principal
// ---------------------------------------------------------------------------
void Engine::audioDeviceAboutToStart (juce::AudioIODevice* d)
{
    mixer.running.store (false);
    sr = d->getCurrentSampleRate();
    block = d->getCurrentBufferSizeSamples();
    playHead.rate.store (sr);
    if (! recorder.isRecording()) recorder.prepare (sr);
    prepareAll();
    mixer.prepareOutputs (sr, block);
    if (pcActive.load())
        pcBridge.configure (pcRate, sr, pcDevice->getCurrentBufferSizeSamples() + (int) (settings.pcBufferMs * pcRate / 1000.0));
    if (streamActive.load())
        streamBridge.configure (sr, streamRate, block + (int) (0.01 * sr));
    mixer.running.store (true);
}

void Engine::audioDeviceStopped()
{
    mixer.running.store (false);
}

void Engine::audioDeviceError (const String& msg)
{
    log ("Error de audio: " + msg);
    juce::MessageManager::callAsync ([this, msg] {
        if (sendEvent) sendEvent (juce::JSON::toString (Obj().set ("event", "error").set ("message", msg), true));
    });
}

void Engine::audioDeviceIOCallbackWithContext (const float* const* in, int numIn, float* const* out, int numOut, int n,
                                               const juce::AudioIODeviceCallbackContext&)
{
    const auto t0 = juce::Time::getHighResolutionTicks();
    mixer.process (in, numIn, out, numOut, n, pcActive.load(), pcBridge, streamActive.load() ? &streamBridge : nullptr);
    if (numOut > 0) recorder.pushBlock (out[0], numOut > 1 ? out[1] : nullptr, n);   // graba lo que escuchas
    playHead.samples.fetch_add (n);
    const double ms = juce::Time::highResolutionTicksToSeconds (juce::Time::getHighResolutionTicks() - t0) * 1000.0;
    if (ms > maxProcMs.load()) maxProcMs.store (ms);
    if (ms > 1000.0 * n / sr) lateBlocks.fetch_add (1);   // más que el tiempo del bloque = corte seguro
}

// ---------------------------------------------------------------------------
// Sesión
// ---------------------------------------------------------------------------
String Engine::newId (const char* prefix)
{
    return String (prefix) + String::toHexString ((juce::int64) juce::Time::currentTimeMillis()).getLastCharacters (6)
           + String (++idCounter);
}

StripModel* Engine::findStrip (const String& id)
{
    for (auto& s : strips)
        if (s.id == id || s.name == id) return &s;
    return nullptr;
}

SlotPtr Engine::findSlot (StripModel& s, const String& uid, int* index)
{
    for (size_t i = 0; i < s.chain.size(); ++i)
        if (s.chain[i]->uid == uid)
        {
            if (index) *index = (int) i;
            return s.chain[i];
        }
    return nullptr;
}

void Engine::prepareSlot (Slot& s)
{
    if (s.fx) s.fx->prepare (sr, kMaxBlock);
    if (s.plugin)
    {
        s.plugin->inst->setPlayHead (&playHead);
        s.plugin->prepare (sr, std::max (block, 512));
    }
}

void Engine::prepareAll()
{
    for (auto& s : strips)
    {
        s.st->gainSmooth.reset (sr, 0.02);
        s.st->gainSmooth.setCurrentAndTargetValue (s.st->gain.load());
        for (auto& slot : s.chain) prepareSlot (*slot);
    }
}

void Engine::applyStripStateToAtomics (StripModel& s)
{
    auto& st = *s.st;
    st.gain.store (toDbGain (s.gainDb));
    st.pan.store (s.pan);
    st.mute.store (s.mute);
    st.solo.store (s.solo);
    st.toMonitor.store (s.toMonitor);
    st.toStream.store (s.toStream);
    for (auto& [bus, lvl] : s.sends) st.send (bus).store (lvl);
}

void Engine::rebuildPatch()
{
    auto p = std::make_unique<Patch>();
    std::map<String, int> busPos, inputIdx;
    for (auto& s : strips)
    {
        if (s.kind != "input") continue;
        auto ps = std::make_unique<PatchStrip>();
        ps->id = s.id;
        ps->sourceKind = s.inputMode == "asio" ? 1 : (s.inputMode == "capture" ? 2 : 0);
        for (int c : s.channels) ps->channels.push_back (c);
        ps->stereoChannels = s.stereo;
        ps->st = s.st;
        ps->chain = s.chain;
        inputIdx[s.name] = (int) p->strips.size();
        p->strips.push_back (std::move (ps));
    }
    p->numInputs = (int) p->strips.size();
    for (auto& s : strips)
    {
        if (s.kind != "bus") continue;
        auto ps = std::make_unique<PatchStrip>();
        ps->id = s.id;
        ps->isBus = true;
        ps->st = s.st;
        ps->chain = s.chain;
        busPos[s.id] = (int) p->strips.size();
        p->strips.push_back (std::move (ps));
    }
    for (auto& s : strips)
    {
        if (s.kind != "input") continue;
        PatchStrip* ps = nullptr;
        for (auto& x : p->strips) if (x->id == s.id) ps = x.get();
        for (auto& [bus, lvl] : s.sends)
        {
            auto it = busPos.find (bus);
            if (it != busPos.end()) ps->sends.push_back ({ it->second, &s.st->send (bus) });
        }
    }
    for (auto& s : strips)
        for (auto& slot : s.chain)
            if (slot->fx && String (slot->fx->type()) == "ducker")
            {
                auto it = inputIdx.find (slot->fx->sourceName);
                slot->fx->sourceIndex.store (it != inputIdx.end() ? it->second : -1);
            }
    for (auto& s : strips) applyStripStateToAtomics (s);
    mixer.setPatch (std::move (p));
    markDirty();
}

SlotPtr Engine::createBuiltinSlot (const String& type)
{
    auto fx = createBuiltin (type);
    if (! fx) return nullptr;
    auto slot = std::make_shared<Slot>();
    slot->uid = newId ("fx");
    slot->type = type;
    slot->fx = std::move (fx);
    prepareSlot (*slot);
    return slot;
}

SlotPtr Engine::createPluginSlot (const String& path, const String& name, String& error)
{
    juce::OwnedArray<juce::PluginDescription> found;
    for (auto* f : formats.getFormats())
        if (f->fileMightContainThisPluginType (path))
            f->findAllTypesForFile (found, path);
    if (found.isEmpty())
    {
        error = juce::String::fromUTF8 ("No encontré un plugin válido en ") + path;
        return nullptr;
    }
    juce::PluginDescription* desc = found[0];
    for (auto* d : found)
        if (name.isNotEmpty() && d->name == name) desc = d;

    auto inst = formats.createPluginInstance (*desc, sr, std::max (block, 512), error);
    if (inst == nullptr)
    {
        if (error.isEmpty()) error = "El plugin no se pudo crear";
        return nullptr;
    }
    // Bus principal en estéreo si lo soporta; buses extra (sidechain) apagados
    auto layout = inst->getBusesLayout();
    if (layout.inputBuses.size() > 0) layout.inputBuses.getReference (0) = juce::AudioChannelSet::stereo();
    if (layout.outputBuses.size() > 0) layout.outputBuses.getReference (0) = juce::AudioChannelSet::stereo();
    for (int b = 1; b < layout.inputBuses.size(); ++b) layout.inputBuses.getReference (b) = juce::AudioChannelSet::disabled();
    if (inst->checkBusesLayoutSupported (layout)) inst->setBusesLayout (layout);

    auto slot = std::make_shared<Slot>();
    slot->uid = newId ("vst");
    slot->type = "vst3";
    slot->plugin = std::make_unique<PluginSlot>();
    slot->plugin->inst = std::move (inst);
    slot->plugin->path = path;
    slot->plugin->name = desc->name;
    slot->pluginPath = path;
    slot->pluginName = desc->name;
    prepareSlot (*slot);
    log ("Plugin cargado: " + desc->name + " (" + path + ") in=" + String (slot->plugin->ins)
         + " out=" + String (slot->plugin->outs) + " latencia=" + String (slot->plugin->inst->getLatencySamples()));
    {
        const auto& ps = slot->plugin->inst->getParameters();
        String list;
        int shown = 0;
        for (int i = 0; i < ps.size() && shown < 80; ++i)
        {
            const String n = ps[i]->getName (40);
            if (n.startsWithIgnoreCase ("MIDI CC")) continue;
            list << "\n    [" << i << "] " << n << " = " << ps[i]->getText (ps[i]->getValue(), 40)
                 << " (pasos " << ps[i]->getNumSteps() << ")";
            ++shown;
        }
        log (juce::String::fromUTF8 ("Parámetros de ") + desc->name + ":" + list);
    }
    return slot;
}

var Engine::slotJson (const Slot& s, bool withState)
{
    Obj o;
    o.set ("uid", s.uid).set ("type", s.type).set ("title", s.title())
     .set ("enabled", s.enabled.load()).set ("failed", s.failed.load()).set ("error", s.error);
    if (s.fx)
    {
        Obj params;
        const auto& specs = s.fx->specs();
        for (size_t i = 0; i < specs.size(); ++i)
        {
            if (String (specs[i].kind) == "source") params.set (specs[i].id, s.fx->sourceName);
            else if (String (specs[i].kind) == "bool") params.set (specs[i].id, s.fx->get ((int) i) > 0.5f);
            else params.set (specs[i].id, s.fx->get ((int) i));
        }
        o.set ("params", params);
    }
    if (s.type == "vst3")
    {
        o.set ("path", s.pluginPath).set ("plugin_name", s.pluginName);
        if (s.plugin)
            o.set ("latency", s.plugin->inst->getLatencySamples()).set ("has_editor", s.plugin->inst->hasEditor());
        if (withState)
        {
            juce::MemoryBlock mb;
            if (s.plugin) s.plugin->inst->getStateInformation (mb);
            else mb = s.savedState;
            if (mb.getSize() > 0) o.set ("state", mb.toBase64Encoding());
        }
    }
    return o;
}

var Engine::stripJson (const StripModel& s, bool withState)
{
    Obj o;
    juce::Array<var> chain;
    for (auto& slot : s.chain) chain.add (slotJson (*slot, withState));
    Obj sends;
    for (auto& [k, v] : s.sends) sends.set (k.toRawUTF8(), v);
    o.set ("id", s.id).set ("name", s.name).set ("kind", s.kind).set ("source", s.source)
     .set ("input_mode", s.inputMode).set ("channels", intArray (s.channels)).set ("stereo", s.stereo)
     .set ("gain_db", s.gainDb).set ("pan", s.pan).set ("mute", s.mute).set ("solo", s.solo)
     .set ("outputs", Obj().set ("monitor", s.toMonitor).set ("stream", s.toStream))
     .set ("sends", sends).set ("chain", chain);
    return o;
}

var Engine::stateJson (bool withState)
{
    juce::Array<var> st;
    for (auto& s : strips) st.add (stripJson (s, withState));
    auto out = [] (const OutputModel& m) { return (var) Obj().set ("gain_db", m.gainDb).set ("mute", m.mute).set ("limiter", m.limiter); };
    Obj audio;
    audio.set ("driver_type", settings.driverType).set ("device", settings.device)
         .set ("sample_rate", settings.sampleRate).set ("buffer_size", settings.bufferSize)
         .set ("monitor_left", settings.monitorLeft).set ("pc_device", settings.pcDevice)
         .set ("stream_device", settings.streamDevice).set ("pc_buffer_ms", settings.pcBufferMs)
         .set ("multithread", settings.multithread);
    return Obj().set ("version", 1).set ("audio", audio).set ("strips", st)
                .set ("outputs", Obj().set ("monitor", out (outMonitor)).set ("stream", out (outStream)));
}

SlotPtr Engine::slotFromJson (const var& v)
{
    const String type = v["type"].toString();
    SlotPtr slot;
    if (type == "vst3")
    {
        String err;
        const String path = v["path"].toString(), pname = v["plugin_name"].toString();
        slot = createPluginSlot (path, pname, err);
        juce::MemoryBlock mb;
        if (v["state"].isString()) mb.fromBase64Encoding (v["state"].toString());
        if (slot == nullptr)
        {
            slot = std::make_shared<Slot>();
            slot->uid = newId ("vst");
            slot->type = "vst3";
            slot->pluginPath = path;
            slot->pluginName = pname;
            slot->savedState = mb;
            slot->failed.store (true);
            slot->error = err;
            log (juce::String::fromUTF8 ("No cargó ") + pname + ": " + err);
        }
        else if (mb.getSize() > 0)
            slot->plugin->inst->setStateInformation (mb.getData(), (int) mb.getSize());
    }
    else
    {
        slot = createBuiltinSlot (type);
        if (slot == nullptr) return nullptr;
        if (auto* params = v["params"].getDynamicObject())
            for (auto& p : params->getProperties())
            {
                const int i = slot->fx->indexOf (p.name.toString());
                if (i < 0) continue;
                if (String (slot->fx->specs()[(size_t) i].kind) == "source") slot->fx->sourceName = p.value.toString();
                else slot->fx->values[(size_t) i].store ((float) p.value);
            }
    }
    slot->enabled.store (v.hasProperty ("enabled") ? (bool) v["enabled"] : true);
    return slot;
}

void Engine::applyStripJson (StripModel& s, const var& v)
{
    if (has (v, "name")) s.name = v["name"].toString();
    if (has (v, "gain_db")) s.gainDb = (float) v["gain_db"];
    if (has (v, "pan")) s.pan = (float) v["pan"];
    if (has (v, "mute")) s.mute = (bool) v["mute"];
    if (has (v, "solo")) s.solo = (bool) v["solo"];
    if (has (v, "input_mode")) s.inputMode = v["input_mode"].toString();
    if (has (v, "stereo")) s.stereo = (bool) v["stereo"];
    if (auto* arr = v["channels"].getArray())
    {
        s.channels.clear();
        for (auto& c : *arr) s.channels.add ((int) c);
    }
    if (has (v, "outputs"))
    {
        if (v["outputs"].hasProperty ("monitor")) s.toMonitor = (bool) v["outputs"]["monitor"];
        if (v["outputs"].hasProperty ("stream")) s.toStream = (bool) v["outputs"]["stream"];
    }
    if (auto* sends = v["sends"].getDynamicObject())
        for (auto& p : sends->getProperties()) s.sends[p.name.toString()] = (float) p.value;
}

void Engine::createDefaultSession()
{
    strips.clear();
    StripModel mic;
    mic.id = "mic"; mic.name = "Mic"; mic.kind = "input"; mic.source = "mic"; mic.inputMode = "asio"; mic.channels = { 0 };
    auto gate = createBuiltinSlot ("gate"); gate->enabled.store (false);
    auto eq = createBuiltinSlot ("eq"); eq->fx->values[0].store (90.0f);
    auto comp = createBuiltinSlot ("compressor");
    mic.chain = { gate, eq, comp };

    StripModel pc;
    pc.id = "pc"; pc.name = "PC"; pc.kind = "input"; pc.source = "pc"; pc.inputMode = "capture";
    auto duck = createBuiltinSlot ("ducker"); duck->enabled.store (false); duck->fx->sourceName = "Mic";
    pc.chain = { duck };

    StripModel rev;
    rev.id = "reverb"; rev.name = "Reverb"; rev.kind = "bus"; rev.inputMode = "none";
    rev.chain = { createBuiltinSlot ("reverb") };
    StripModel dly;
    dly.id = "delay"; dly.name = "Delay"; dly.kind = "bus"; dly.inputMode = "none";
    dly.chain = { createBuiltinSlot ("delay") };
    mic.sends = { { "reverb", 0.2f }, { "delay", 0.1f } };

    strips.push_back (std::move (mic));
    strips.push_back (std::move (pc));
    strips.push_back (std::move (rev));
    strips.push_back (std::move (dly));
}

void Engine::loadSession()
{
    auto f = configDir().getChildFile ("engine_session.json");
    var v = f.existsAsFile() ? juce::JSON::parse (f) : var();
    if (! v.isObject())
    {
        createDefaultSession();
        return;
    }
    const var a = v["audio"];
    if (a.isObject())
    {
        settings.driverType = a["driver_type"].toString().isNotEmpty() ? a["driver_type"].toString() : settings.driverType;
        settings.device = a["device"].toString();
        if ((double) a["sample_rate"] > 0) settings.sampleRate = (double) a["sample_rate"];
        if ((int) a["buffer_size"] > 0) settings.bufferSize = (int) a["buffer_size"];
        settings.monitorLeft = (int) a["monitor_left"];
        if (a.hasProperty ("pc_device")) settings.pcDevice = a["pc_device"].toString();
        if (a.hasProperty ("stream_device")) settings.streamDevice = a["stream_device"].toString();
        if ((int) a["pc_buffer_ms"] > 0) settings.pcBufferMs = (int) a["pc_buffer_ms"];
        settings.multithread = (bool) a["multithread"];
    }
    auto readOut = [] (OutputModel& m, const var& o)
    {
        if (! o.isObject()) return;
        m.gainDb = (float) o["gain_db"]; m.mute = (bool) o["mute"];
        m.limiter = o.hasProperty ("limiter") ? (bool) o["limiter"] : true;
    };
    readOut (outMonitor, v["outputs"]["monitor"]);
    readOut (outStream, v["outputs"]["stream"]);

    loadStrips (v);
}

void Engine::loadStrips (const var& v)
{
    strips.clear();
    if (auto* arr = v["strips"].getArray())
        for (auto& sv : *arr)
        {
            StripModel s;
            s.id = sv["id"].toString();
            s.kind = sv["kind"].toString().isNotEmpty() ? sv["kind"].toString() : "input";
            s.source = sv["source"].toString();
            applyStripJson (s, sv);
            if (auto* chain = sv["chain"].getArray())
                for (auto& cv : *chain)
                    if (auto slot = slotFromJson (cv)) s.chain.push_back (slot);
            strips.push_back (std::move (s));
        }
    if (strips.empty()) createDefaultSession();
}

void Engine::saveSession()
{
    auto f = configDir().getChildFile ("engine_session.json");
    auto tmp = f.getSiblingFile ("engine_session.tmp");
    if (tmp.replaceWithText (juce::JSON::toString (stateJson (true))))
        tmp.moveFileTo (f);
    dirtyAt = 0;
}

// ---------------------------------------------------------------------------
// Plugins
// ---------------------------------------------------------------------------
var Engine::pluginParams (Slot& s)
{
    juce::Array<var> out;
    if (! s.plugin) return out;
    const auto& params = s.plugin->inst->getParameters();
    for (int i = 0; i < params.size(); ++i)
    {
        auto* p = params[i];
        const String name = p->getName (64);
        if (name.startsWithIgnoreCase ("MIDI CC") || name.isEmpty()) continue;
        out.add (Obj().set ("index", i).set ("name", name).set ("value", p->getValue())
                      .set ("text", p->getText (p->getValue(), 64)).set ("label", p->getLabel())
                      .set ("steps", p->getNumSteps()).set ("discrete", p->isDiscrete()));
    }
    return out;
}

void Engine::openEditor (const String& stripName, int colourIndex, SlotPtr slot)
{
    if (auto it = windows.find (slot->uid); it != windows.end())
    {
        it->second->setVisible (true);
        it->second->setMinimised (false);
        it->second->toFront (true);
        return;
    }
    auto* ed = slot->plugin->inst->createEditorIfNeeded();
    if (ed == nullptr) return;
    const String uid = slot->uid;
    windows[uid] = std::make_unique<PluginWindow> (slot->title() + juce::String::fromUTF8 ("  —  ") + stripName, ed, [this, uid] {
        juce::MessageManager::callAsync ([this, uid] { closeEditor (uid); });
    });
    PluginWindowStyle::apply (*windows[uid], colourIndex, stripName, slot->title());
}

void Engine::closeEditor (const String& uid)
{
    windows.erase (uid);
}

// ---------------------------------------------------------------------------
// Comandos
// ---------------------------------------------------------------------------
var Engine::handle (const var& m)
{
    const String cmd = m["cmd"].toString();
    try
    {
        if (cmd == "hello")
            return ok (Obj().set ("engine", "VocalEngine").set ("version", "1.0").set ("juce", juce::SystemStats::getJUCEVersion()));

        if (cmd == "describe")
        {
            juce::Array<var> types;
            for (auto& t : builtinTypes())
            {
                auto fx = createBuiltin (t);
                juce::Array<var> params;
                for (auto& sp : fx->specs())
                {
                    juce::Array<var> choices;
                    for (auto* c : sp.choices) choices.add (String (juce::CharPointer_UTF8 (c)));
                    params.add (Obj().set ("id", sp.id).set ("label", String (juce::CharPointer_UTF8 (sp.label)))
                                     .set ("kind", sp.kind).set ("min", sp.min).set ("max", sp.max).set ("default", sp.def)
                                     .set ("unit", String (juce::CharPointer_UTF8 (sp.unit))).set ("log", sp.log)
                                     .set ("decimals", sp.decimals).set ("choices", choices));
                }
                types.add (Obj().set ("type", t).set ("label", String (juce::CharPointer_UTF8 (fx->label())))
                                .set ("category", String (juce::CharPointer_UTF8 (fx->category()))).set ("params", params));
            }
            return ok (types);
        }

        if (cmd == "get_state") return ok (stateJson (false));
        if (cmd == "audio_devices") return ok (describeDevices());

        if (cmd == "set_audio")
        {
            if (recorder.isRecording()) return fail (juce::String::fromUTF8 ("Detén la grabación antes de cambiar el audio"));
            if (has (m, "driver_type")) settings.driverType = m["driver_type"].toString();
            if (has (m, "device")) settings.device = m["device"].toString();
            if (has (m, "sample_rate")) settings.sampleRate = (double) m["sample_rate"];
            if (has (m, "buffer_size")) settings.bufferSize = (int) m["buffer_size"];
            if (has (m, "monitor_left")) settings.monitorLeft = (int) m["monitor_left"];
            if (has (m, "pc_device")) settings.pcDevice = m["pc_device"].toString();
            if (has (m, "stream_device")) settings.streamDevice = m["stream_device"].toString();
            if (has (m, "pc_buffer_ms")) settings.pcBufferMs = juce::jlimit (2, 200, (int) m["pc_buffer_ms"]);
            closeSecondary();
            audioError = openAudio();
            openSecondary();
            rebuildPatch();
            saveSession();
            return ok (describeDevices());
        }

        if (cmd == "asio_panel")
        {
            auto* d = dm.getCurrentAudioDevice();
            if (d == nullptr || ! d->hasControlPanel()) return fail ("Este dispositivo no tiene panel de control");
            if (d->showControlPanel())
            {
                // el driver cambió algo (buffer/frecuencia): reabrir
                audioError = openAudio();
            }
            return ok (describeDevices());
        }

        if (cmd == "set_output")
        {
            auto& model = m["output"].toString() == "stream" ? outStream : outMonitor;
            auto& st = m["output"].toString() == "stream" ? mixer.streamOut : mixer.monitorOut;
            if (has (m, "gain_db")) model.gainDb = (float) m["gain_db"];
            if (has (m, "mute")) model.mute = (bool) m["mute"];
            if (has (m, "limiter")) model.limiter = (bool) m["limiter"];
            st.gain.store (toDbGain (model.gainDb));
            st.mute.store (model.mute);
            st.limiter.store (model.limiter);
            markDirty();
            return ok();
        }

        if (cmd == "set_tempo")
        {
            playHead.bpm.store (juce::jlimit (30.0, 300.0, (double) m["bpm"]));
            return ok();
        }

        if (cmd == "add_strip")
        {
            StripModel s;
            s.id = newId ("bus");
            s.kind = m["kind"].toString().isNotEmpty() ? m["kind"].toString() : "bus";
            s.name = m["name"].toString().isNotEmpty() ? m["name"].toString() : "Bus";
            s.inputMode = s.kind == "bus" ? "none" : "asio";
            s.source = m["source"].toString();
            if (s.kind == "input" && ! has (m, "channels")) s.channels = { 1 };
            applyStripJson (s, m);
            if (auto* fx = m["fx"].getArray())
                for (auto& t : *fx)
                    if (auto slot = createBuiltinSlot (t.toString())) s.chain.push_back (slot);
            strips.push_back (std::move (s));
            rebuildPatch();
            return ok (stripJson (strips.back(), false));
        }

        // a partir de aquí todos los comandos apuntan a un canal
        auto* s = findStrip (m["strip"].toString());

        if (cmd == "set_strip")
        {
            if (! s) return fail ("canal no encontrado");
            const bool topo = has (m, "channels") || has (m, "input_mode") || has (m, "sends") || has (m, "stereo") || has (m, "name");
            applyStripJson (*s, m);
            applyStripStateToAtomics (*s);
            if (topo) rebuildPatch();
            else markDirty();
            return ok();
        }

        if (cmd == "remove_strip")
        {
            if (! s) return fail ("canal no encontrado");
            if (s->id == "mic" || s->id == "pc") return fail ("El canal principal no se puede quitar");
            const String id = s->id;
            for (auto& slot : s->chain) closeEditor (slot->uid);
            strips.erase (std::remove_if (strips.begin(), strips.end(), [&] (const StripModel& x) { return x.id == id; }), strips.end());
            for (auto& o : strips) o.sends.erase (id);
            rebuildPatch();
            return ok();
        }

        if (cmd == "add_fx" || cmd == "add_plugin")
        {
            if (! s) return fail ("canal no encontrado");
            SlotPtr slot;
            if (cmd == "add_fx")
            {
                slot = createBuiltinSlot (m["type"].toString());
                if (! slot) return fail ("Tipo de efecto desconocido: " + m["type"].toString());
                if (slot->fx && String (slot->fx->type()) == "ducker") slot->fx->sourceName = "Mic";
            }
            else
            {
                String err;
                slot = createPluginSlot (m["path"].toString(), m["name"].toString(), err);
                if (! slot) return fail (err);
            }
            const int idx = has (m, "index") ? juce::jlimit (0, (int) s->chain.size(), (int) m["index"]) : (int) s->chain.size();
            auto chain = s->chain;
            chain.insert (chain.begin() + idx, slot);
            s->chain = chain;
            rebuildPatch();
            return ok (slotJson (*slot, false));
        }

        if (cmd == "remove_fx" || cmd == "move_fx" || cmd == "set_fx" || cmd == "plugin_params"
            || cmd == "set_plugin_param" || cmd == "set_plugin_param_text" || cmd == "open_editor" || cmd == "get_fx")
        {
            if (! s) return fail ("canal no encontrado");
            int idx = -1;
            auto slot = findSlot (*s, m["slot"].toString(), &idx);
            if (! slot) return fail ("efecto no encontrado");

            if (cmd == "get_fx") return ok (slotJson (*slot, false));

            if (cmd == "remove_fx")
            {
                closeEditor (slot->uid);
                auto chain = s->chain;
                chain.erase (chain.begin() + idx);
                s->chain = chain;
                rebuildPatch();
                return ok();
            }
            if (cmd == "move_fx")
            {
                auto chain = s->chain;
                chain.erase (chain.begin() + idx);
                const int to = juce::jlimit (0, (int) chain.size(), (int) m["index"]);
                chain.insert (chain.begin() + to, slot);
                s->chain = chain;
                rebuildPatch();
                return ok();
            }
            if (cmd == "set_fx")
            {
                bool rebuild = false;
                if (has (m, "enabled"))
                {
                    slot->enabled.store ((bool) m["enabled"]);
                    if (slot->fx) slot->fx->reset();
                }
                if (slot->fx)
                    if (auto* params = m["params"].getDynamicObject())
                        for (auto& p : params->getProperties())
                        {
                            const int i = slot->fx->indexOf (p.name.toString());
                            if (i < 0) continue;
                            const String kind = slot->fx->specs()[(size_t) i].kind;
                            if (kind == "source") { slot->fx->sourceName = p.value.toString(); rebuild = true; }
                            else if (kind == "bool") slot->fx->values[(size_t) i].store ((bool) p.value ? 1.0f : 0.0f);
                            else slot->fx->values[(size_t) i].store ((float) p.value);
                        }
                if (rebuild) rebuildPatch();
                else markDirty();
                return ok();
            }
            if (! slot->plugin) return fail (slot->error.isNotEmpty() ? slot->error : "No es un plugin");
            if (cmd == "plugin_params") return ok (pluginParams (*slot));
            if (cmd == "open_editor")
            {
                if (! slot->plugin->inst->hasEditor()) return fail ("Este plugin no tiene interfaz propia");
                int colour = 3;
                if (s->kind == "input")
                    colour = s->source == "pc" ? 2 : (s->id == "mic" ? 0 : 1);
                openEditor (s->name, colour, slot);
                return ok();
            }
            auto& params = slot->plugin->inst->getParameters();
            const int pi = (int) m["index"];
            if (pi < 0 || pi >= params.size()) return fail (juce::String::fromUTF8 ("parámetro inválido"));
            auto* p = params[pi];
            if (cmd == "set_plugin_param")
            {
                p->setValueNotifyingHost (juce::jlimit (0.0f, 1.0f, (float) m["value"]));
                markDirty();
                return ok (Obj().set ("text", p->getText (p->getValue(), 64)).set ("value", p->getValue()));
            }
            if (cmd == "set_plugin_param_text")
            {
                juce::StringArray wanted;
                if (auto* arr = m["texts"].getArray())
                    for (auto& t : *arr) wanted.add (t.toString().removeCharacters (" ").toLowerCase());
                int steps = p->getNumSteps();
                if (steps <= 1 || steps > 4096) steps = 1000;
                for (int k = 0; k < steps; ++k)
                {
                    const float v = steps > 1 ? (float) k / (float) (steps - 1) : 0.0f;
                    const String txt = p->getText (v, 64).trim();
                    const String norm = txt.removeCharacters (" ").toLowerCase();
                    bool match = wanted.contains (norm);
                    if (! match)
                    {
                        juce::StringArray tok;
                        tok.addTokens (txt.toLowerCase(), "/|, ", "");
                        tok.removeEmptyStrings();
                        for (auto& t : tok)
                            if (wanted.contains (t) && tok.size() <= 3) { match = true; break; }
                    }
                    if (match)
                    {
                        p->setValueNotifyingHost (v);
                        markDirty();
                        return ok (Obj().set ("text", txt).set ("value", v));
                    }
                }
                // Parámetro continuo (Hz, dB, %, ms…): convertir el número buscando el valor del plugin
                const String first = m["texts"].getArray() && m["texts"].getArray()->size() > 0 ? (*m["texts"].getArray())[0].toString() : String();
                const auto numOf = [] (const String& t) { return t.retainCharacters ("0123456789.,-+").replaceCharacter (',', '.').getDoubleValue(); };
                const bool looksNumeric = first.trim().isNotEmpty() && first.trim().containsOnly ("0123456789.,-+ ");
                if (looksNumeric)
                {
                    const double target = numOf (first);
                    // 1) lo que el plugin entiende directo
                    float v = p->getValueForText (first);
                    double got = numOf (p->getText (v, 64));
                    if (std::abs (got - target) > std::max (0.02, std::abs (target) * 0.01))
                    {
                        // 2) búsqueda binaria (los parámetros continuos son monótonos)
                        float lo = 0.0f, hi = 1.0f;
                        const bool rising = numOf (p->getText (1.0f, 64)) >= numOf (p->getText (0.0f, 64));
                        for (int it = 0; it < 40; ++it)
                        {
                            const float mid = 0.5f * (lo + hi);
                            const double mv = numOf (p->getText (mid, 64));
                            if ((mv < target) == rising) lo = mid; else hi = mid;
                        }
                        v = 0.5f * (lo + hi);
                        got = numOf (p->getText (v, 64));
                    }
                    if (std::abs (got - target) <= std::max (0.05, std::abs (target) * 0.02))
                    {
                        p->setValueNotifyingHost (v);
                        markDirty();
                        return ok (Obj().set ("text", p->getText (v, 64)).set ("value", v));
                    }
                }
                return fail (juce::String::fromUTF8 ("ningún valor coincide"));
            }
        }

        if (cmd == "save") { saveSession(); return ok(); }
        if (cmd == "set_multithread")
        {
            settings.multithread = (bool) m["on"];
            mixer.multithread.store (settings.multithread);
            markDirty();
            return ok();
        }
        if (cmd == "rec_start")
        {
            String err;
            if (! recorder.start (juce::File (m["path"].toString()), err)) return fail (err);
            log ("Grabando en " + m["path"].toString());
            return ok();
        }
        if (cmd == "rec_marker")
            return ok (Obj().set ("t", recorder.marker (m["name"].toString())));
        if (cmd == "rec_stop")
        {
            auto r = recorder.stop();
            juce::Array<var> marks;
            for (auto& mk : r.markers) marks.add (Obj().set ("t", mk.seconds).set ("name", mk.name));
            log ("Grabación detenida: " + String (r.seconds, 1) + " s, perdidas " + String (r.dropped));
            if (! r.ok) return fail (r.error);
            return ok (Obj().set ("seconds", r.seconds).set ("frames", r.frames).set ("dropped", r.dropped)
                            .set ("path", r.path).set ("markers", marks));
        }
        if (cmd == "get_session") return ok (stateJson (true));
        if (cmd == "load_session")
        {
            const var data = m["data"];
            if (! data.isObject() || ! data["strips"].isArray()) return fail ("preset inválido");
            windows.clear();
            loadStrips (data);
            auto readOut = [] (OutputModel& om, const var& o)
            {
                if (! o.isObject()) return;
                om.gainDb = (float) o["gain_db"]; om.mute = (bool) o["mute"];
                om.limiter = o.hasProperty ("limiter") ? (bool) o["limiter"] : true;
            };
            readOut (outMonitor, data["outputs"]["monitor"]);
            readOut (outStream, data["outputs"]["stream"]);
            for (auto [model, st] : { std::pair<OutputModel*, OutputState*> { &outMonitor, &mixer.monitorOut },
                                      std::pair<OutputModel*, OutputState*> { &outStream, &mixer.streamOut } })
            {
                st->gain.store (toDbGain (model->gainDb));
                st->mute.store (model->mute);
                st->limiter.store (model->limiter);
            }
            rebuildPatch();
            saveSession();
            return ok (stateJson (false));
        }
        if (cmd == "quit")
        {
            saveSession();
            juce::MessageManager::callAsync ([this] { if (requestQuit) requestQuit(); });
            return ok();
        }
        if (cmd == "plugin_types")
        {
            juce::OwnedArray<juce::PluginDescription> found;
            for (auto* f : formats.getFormats()) f->findAllTypesForFile (found, m["path"].toString());
            juce::Array<var> names;
            for (auto* d : found) names.add (d->name);
            return ok (names);
        }
        return fail ("comando desconocido: " + cmd);
    }
    catch (const std::exception& e)
    {
        log (juce::String::fromUTF8 ("Excepción en ") + cmd + ": " + e.what());
        return fail (String ("Error interno: ") + e.what());
    }
}

// ---------------------------------------------------------------------------
void Engine::changeListenerCallback (juce::ChangeBroadcaster*)
{
    if (sendEvent) sendEvent (juce::JSON::toString (Obj().set ("event", "audio_changed"), true));
}

void Engine::timerCallback()
{
    if (meterTick == 0) log ("Primer tick del temporizador");
    mixer.collectGarbage();
    sendMeters();
    if (++meterTick % 30 == 0 && dirtyAt != 0 && juce::Time::getMillisecondCounter() - dirtyAt > 1500)
        saveSession();
}

void Engine::sendMeters()
{
    if (! sendEvent) return;
    Obj stripsObj, slotsObj;
    for (auto& s : strips)
    {
        stripsObj.set (s.id.toRawUTF8(), Obj().set ("peak", s.st->peak.exchange (0.0f)).set ("in", s.st->inPeak.exchange (0.0f))
                                               .set ("clips", s.st->clips.load()).set ("cpu_us", s.st->procUs.exchange (0.0f)));
        for (auto& slot : s.chain)
            if (slot->fx && String (slot->fx->type()) == "ducker")
                slotsObj.set (slot->uid.toRawUTF8(), slot->fx->meter.load());
    }
    auto outJ = [] (OutputState& o)
    {
        return (var) Obj().set ("peak", o.peak.exchange (0.0f)).set ("pre", o.prePeak.exchange (0.0f)).set ("hits", o.limitHits.load());
    };
    Obj stats;
    if (auto* d = dm.getCurrentAudioDevice())
    {
        const double rt = (d->getInputLatencyInSamples() + d->getOutputLatencyInSamples()) * 1000.0 / std::max (1.0, sr);
        stats.set ("device", d->getName()).set ("type", d->getTypeName())
             .set ("sr", sr).set ("block", block).set ("latency_ms", rt)
             .set ("xruns", d->getXRunCount()).set ("cpu", dm.getCpuUsage());
    }
    stats.set ("late", lateBlocks.load()).set ("proc_max_ms", maxProcMs.exchange (0.0))
         .set ("pc_active", pcActive.load()).set ("pc_ms", pcBridge.latencyMs (pcRate))
         .set ("pc_under", pcBridge.underruns.load()).set ("pc_over", pcBridge.overruns.load())
         .set ("pc_ratio", pcBridge.currentRatio.load())
         .set ("stream_active", streamActive.load()).set ("stream_under", streamBridge.underruns.load())
         .set ("pc_sr", pcRate).set ("multithread", mixer.multithread.load()).set ("parallel", mixer.lastParallel.load())
         .set ("rec", Obj().set ("on", recorder.isRecording()).set ("t", recorder.elapsedSeconds())
                           .set ("dropped", recorder.droppedSamples()));
    sendEvent (juce::JSON::toString (Obj().set ("event", "meters").set ("strips", stripsObj).set ("slots", slotsObj)
                                          .set ("out", Obj().set ("monitor", outJ (mixer.monitorOut)).set ("stream", outJ (mixer.streamOut)))
                                          .set ("stats", stats), true));
}
} // namespace vc
