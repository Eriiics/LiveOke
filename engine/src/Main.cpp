// VocalEngine: motor de audio de baja latencia para VocalChain.
// La interfaz (Python) se conecta por TCP a 127.0.0.1:<puerto> y manda comandos
// JSON, uno por línea. El motor responde {"id":..,"ok":..} y emite eventos
// {"event":"meters",...} ~30 veces por segundo.
#include "Engine.h"

#include <juce_events/juce_events.h>
#include <mutex>

namespace vc
{
class Server : public juce::Thread
{
public:
    Server (Engine& e, int p) : juce::Thread ("vc-server"), engine (e), port (p) {}
    ~Server() override
    {
        signalThreadShouldExit();
        listener.close();
        {
            std::lock_guard<std::mutex> lk (writeMutex);
            if (client) client->close();
        }
        stopThread (2000);
    }

    bool listen() { return listener.createListener (port, "127.0.0.1"); }

    void send (const juce::String& line)
    {
        std::lock_guard<std::mutex> lk (writeMutex);
        if (client == nullptr || ! client->isConnected()) return;
        const auto data = line + "\n";
        const auto* utf8 = data.toRawUTF8();
        const int len = (int) data.getNumBytesAsUTF8();
        int sent = 0;
        while (sent < len)
        {
            const int w = client->write (utf8 + sent, len - sent);
            if (w <= 0) break;
            sent += w;
        }
    }

    std::function<void (bool)> onConnectionChanged;

    void run() override
    {
        engine.log ("Hilo del servidor iniciado");
        while (! threadShouldExit())
        {
            std::unique_ptr<juce::StreamingSocket> c (listener.waitForNextConnection());
            engine.log (c ? juce::String::fromUTF8 ("Conexión aceptada") : juce::String::fromUTF8 ("accept devolvió null"));
            if (c == nullptr) { if (threadShouldExit()) break; wait (50); continue; }
            {
                std::lock_guard<std::mutex> lk (writeMutex);
                client = std::move (c);
            }
            notify (true);
            juce::MemoryBlock pending;
            char buf[8192];
            while (! threadShouldExit())
            {
                juce::StreamingSocket* cl;
                {
                    std::lock_guard<std::mutex> lk (writeMutex);
                    cl = client.get();
                }
                const int ready = cl->waitUntilReady (true, 100);
                if (ready < 0) break;
                if (ready == 0) continue;
                const int r = cl->read (buf, sizeof (buf), false);
                if (r <= 0) break;
                pending.append (buf, (size_t) r);
                for (;;)
                {
                    const auto* d = static_cast<const char*> (pending.getData());
                    const auto size = pending.getSize();
                    size_t nl = 0;
                    while (nl < size && d[nl] != '\n') ++nl;
                    if (nl >= size) break;
                    const juce::String line = juce::String::fromUTF8 (d, (int) nl);
                    pending.removeSection (0, nl + 1);
                    if (line.trim().isNotEmpty()) dispatch (line);
                }
            }
            {
                std::lock_guard<std::mutex> lk (writeMutex);
                client.reset();
            }
            notify (false);
        }
    }

private:
    void notify (bool connected)
    {
        juce::MessageManager::callAsync ([this, connected] { if (onConnectionChanged) onConnectionChanged (connected); });
    }

    void dispatch (const juce::String& line)
    {
        juce::MessageManager::callAsync ([this, line] {
            const auto msg = juce::JSON::parse (line);
            juce::var resp;
            if (! msg.isObject())
                resp = juce::JSON::parse (juce::String::fromUTF8 ("{\"ok\":false,\"error\":\"JSON inválido\"}"));
            else
                resp = engine.handle (msg);
            if (auto* o = resp.getDynamicObject())
                o->setProperty ("id", msg["id"]);
            send (juce::JSON::toString (resp, true));
        });
    }

    Engine& engine;
    int port;
    juce::StreamingSocket listener;
    std::unique_ptr<juce::StreamingSocket> client;
    std::mutex writeMutex;
};

class VocalEngineApp : public juce::JUCEApplication, private juce::Timer
{
public:
    const juce::String getApplicationName() override { return "VocalEngine"; }
    const juce::String getApplicationVersion() override { return "1.0"; }
    bool moreThanOneInstanceAllowed() override { return false; }

    void initialise (const juce::String& cmd) override
    {
        int port = 47800;
        auto args = juce::StringArray::fromTokens (cmd, true);
        for (int i = 0; i < args.size(); ++i)
        {
            if (args[i] == "--port" && i + 1 < args.size()) port = args[i + 1].getIntValue();
            if (args[i] == "--stay") stay = true;
        }
        engine = std::make_unique<Engine>();
        server = std::make_unique<Server> (*engine, port);
        engine->sendEvent = [this] (const juce::String& s) { if (server) server->send (s); };
        engine->requestQuit = [this] { systemRequestedQuit(); };
        server->onConnectionChanged = [this] (bool c)
        {
            connected = c;
            engine->log (c ? "Interfaz conectada" : "Interfaz desconectada");
            if (! c && ! stay) startTimer (8000);   // si la interfaz no vuelve, cerrar
            else stopTimer();
        };
        if (! server->listen())
        {
            engine->log ("No pude abrir el puerto " + juce::String (port) + juce::String::fromUTF8 (" (¿ya hay un motor abierto?)"));
            quit();
            return;
        }
        server->startThread();
        engine->log ("Escuchando en 127.0.0.1:" + juce::String (port));
        juce::MessageManager::callAsync ([this] { engine->log ("Bucle de mensajes activo"); });
        if (! stay) startTimer (20000);   // si nadie se conecta, cerrar
    }

    void shutdown() override
    {
        server.reset();
        engine.reset();
    }

    void systemRequestedQuit() override { quit(); }
    void anotherInstanceStarted (const juce::String&) override {}

private:
    void timerCallback() override
    {
        stopTimer();
        if (! connected) systemRequestedQuit();
    }

    std::unique_ptr<Engine> engine;
    std::unique_ptr<Server> server;
    bool stay = false, connected = false;
};
} // namespace vc

START_JUCE_APPLICATION (vc::VocalEngineApp)
