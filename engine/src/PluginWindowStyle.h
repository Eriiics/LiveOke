// Estilo distintivo para las ventanas de editor de plugins que abre el motor:
// título "VocalChain ▸ Mic 1 ▸ Auto-Tune Pro", color según el canal e icono propio
// (círculo de color con "VC") para distinguirlas en la barra de tareas y en Alt+Tab.
#pragma once
#include "JuceConfig.h"
#include <juce_gui_basics/juce_gui_basics.h>

namespace PluginWindowStyle
{
    inline juce::Colour channelColour (int channelIndex)
    {
        static const juce::uint32 palette[] = { 0xffe0457b,  // Mic 1: magenta
                                                0xff2bb3c0,  // Mic 2: turquesa
                                                0xfff0a030,  // PC / instrumental: naranja
                                                0xff7a6cf0 }; // buses: violeta
        return juce::Colour (palette[(size_t) juce::jlimit (0, 3, channelIndex)]);
    }

    inline juce::Image makeIcon (juce::Colour c, int size = 64)
    {
        juce::Image img (juce::Image::ARGB, size, size, true);
        juce::Graphics g (img);
        g.setColour (c);
        g.fillEllipse (1.0f, 1.0f, (float) size - 2.0f, (float) size - 2.0f);
        g.setColour (juce::Colours::white);
        g.setFont (juce::Font ((float) size * 0.42f, juce::Font::bold));
        g.drawText ("VC", img.getBounds(), juce::Justification::centred, false);
        return img;
    }

    inline void apply (juce::DocumentWindow& w, int channelIndex, const juce::String& channelName,
                       const juce::String& pluginName)
    {
        const auto c = channelColour (channelIndex);
        const juce::String sep = juce::String (juce::CharPointer_UTF8 (" \xe2\x96\xb8 "));
        w.setName ("VocalChain" + sep + channelName + sep + pluginName);
        w.setBackgroundColour (c.darker (0.6f));
        w.setIcon (makeIcon (c));
    }
}
