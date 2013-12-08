#!/bin/env python

"""A software simulation of a theremin.

A 2-dimension area serves for control of the instrument; the user drags the mouse on this area to control the frequency and amplitude.  Several modes are provided that adds virtual "frets" to allow for the playing of the equal tempered tuning (or subsets of it).

For better musical sound run your sound card into a guitar amp or similar.

Requires Python 2.3+ and PyGTK 2.4+ (not tested on anything older).

http://ptheremin.sourceforge.net
"""

import array
import fcntl
import math
import ossaudiodev
import struct
import threading
import time
import wave

import pygtk
pygtk.require('2.0')
import gtk
import pango


SCALES = ("chromatic", "diatonic major", "pentatonic major", "pentatonic minor", "blues")
INIT_FREQ = 20

NAME="PTheremin"
VERSION="0.2.1"


# from "Musical Instrument Design" by Bart Hopkin
sharp = 1.05946
equal_temp_freqs = [16.352, 16.352*sharp, 18.354, 18.354*sharp, 20.602, 21.827, 21.827*sharp, 24.500, 24.500*sharp, 27.500, 27.500*sharp, 30.868]
equal_temp_labels = ['C*', 'C*#', 'D*', 'D*#', 'E*', 'F*', 'F*#', 'G*', 'G*#', 'A*', 'A*#', 'B*']

equal_temp_tuning = zip(equal_temp_labels, equal_temp_freqs)

diatonic_major_intervals = (0, 2, 4, 5, 7, 9, 11)
pentatonic_major_intervals = (0, 2, 4, 7, 9)
pentatonic_minor_intervals = (0, 3, 5, 7, 10)
blues_intervals = (0, 3, 5, 6, 7, 10)

# build up several octaves of notes
NOTES = []
for octave in range(11):
    for label,freq in equal_temp_tuning:
        NOTES.append((label.replace('*', "%d" % octave), (2**octave)*freq))

def just_freqs(notes):
    return [freq for label,freq in notes]

class PlaybackThread(threading.Thread):
    """A thread that manages audio playback."""

    def __init__(self, name, device):
        super(PlaybackThread, self).__init__()
        self.name = name

        self.fs = 44100 # the sample frequency
        self.ft = INIT_FREQ # the base frequency of the instrument
        self.vol = 1

        if device != '/dev/null':
            self.dsp = ossaudiodev.open(device, 'w')
            self.dsp.setparameters(ossaudiodev.AFMT_S16_LE, 1, self.fs)

        self.paused = True
        self.alive = True
        self.recording = array.array('h') # *way* faster than a list for data access

        threading.Thread.__init__(self, name=name)


    def run(self):
        def tone_gen(fs):
            """A tone sample generator."""
            x = 0
            pi = math.pi
            sin = math.sin
            ft = self.ft
            sample = 0
            prev_sample = 0
            while 1:
                prev_sample = sample
                sample = sin(2*pi*ft*x/fs)

                # The idea here is to keep the waveform continuous by only changing
                # the frequency at the end of the previous frequency's period.  And
                # it works!
                if ft != self.ft and 0.01 > sample > -0.01 and prev_sample < sample:
                    ft = self.ft
                    x = 0

                x += 1
                yield sample*self.vol*0.95 # don't max out the range otherwise we clip


        # to optimize loop performance, dereference everything ahead of time
        tone = tone_gen(self.fs)
        write_func = self.dsp.write
        free_func = self.dsp.obuffree
        pack_func = struct.pack

        record_func = self.recording.append

        while self.alive:
            wave = ""
            if not self.paused:
                while not free_func():
                    pass

                clean = tone.next()
                val_f = clean
                val_i = int(val_f*(2**15 - 1))

                sample = pack_func("h", val_i)
                write_func(sample)
                record_func(val_i)
            else:
                time.sleep(0.1)


    def stop(self):
        self.alive = False


    def set_new_freq(self, freq, vol):
        """Updates the input frequency."""
        self.ft = freq
        self.vol = vol


    def get_wav_data(self):
        return self.recording


    def clear_wav_data(self):
        self.recording = []




def iir_2pole(coeff1, coeff2):
    """A two-pole IIR filter generator from that one guy's filter design page that I always use."""

    xv = [0, 0, 0]
    yv = [0, 0, 0]
    
    def iir(sample):
        while 1:
            xv[0] = xv[1]
            xv[1] = xv[2]
            xv[2] = sample
            yv[0] = yv[1]
            yv[1] = yv[2]
            yv[2] = xv[0] + xv[2] - 2*xv[1] + coeff1*yv[0] + coeff2*yv[1]
            yield yv[2]


def discrete_tones(tones):
    """Makes a discrete-tone filter that latches to particular tones."""
       
    def filt(x):
        closest = tones[0]
        err = 500000
        mean = 0
        
        iir = iir_2pole(-.9979871157, 1.997850878)
        
        for i,tone in enumerate(tones):
            tone_err = abs(x - tone)
            if tone_err < err:
                closest = tone
                err = tone_err
            elif tone_err > err:
                if i > 0:
                    mean = (x - closest)/2
                    
                break
                
        return closest + mean

    return filt


class ThereminApp(object):
    """The GUI part of the theremin."""

    def delete_event(self, w, e, d=None): return False


    def destroy(self, w=None, d=None):
        for thread in self.threads.values():
            thread.stop()

        gtk.main_quit()


    # the next 5 functions were ripped from the scribblesimple.py example
    def configure_event(self, widget, event):
        # Create a new backing pixmap of the appropriate size
        x, y, width, height = widget.get_allocation()
        self.pixmap = gtk.gdk.Pixmap(widget.window, width, height)

        self.pixmap.draw_rectangle(widget.get_style().black_gc,
                              True, 0, 0, width, height)

        notes = [(label, int(float(x - self.freq_min)*width/(self.freq_max - self.freq_min))) for label,x in self.discrete_notes]
        root_notes = [(label, int(float(x - self.freq_min)*width/(self.freq_max - self.freq_min))) for label,x in self.root_notes]

        ygrid = height/10

        # this is the "intuitive" way to get the gc to be different colors... why isn't this in the pygtk tutorial???
        gc = widget.window.new_gc()
        gc.foreground = gtk.gdk.colormap_get_system().alloc_color(56360, 56360, 56360)

        # TODO when things are cleaner we need to color the root notes differently
        root_gc = widget.window.new_gc()
        root_gc.foreground = gtk.gdk.colormap_get_system().alloc_color(20000, 60000, 20000)
        root_gc.line_width = 3

        pc = widget.get_pango_context()
        layout = pango.Layout(pc)
        layout.set_font_description(pango.FontDescription("sans 8"))

        for label,x in notes:
            if len(label) == 3:
                l = label[0] + label[2]
            else:
                l = label[0]

            layout.set_text(l)

            if (label,x) in root_notes:
                self.pixmap.draw_line(root_gc, x, 0, x, height)
                self.pixmap.draw_layout(root_gc, x + 2, 0, layout)
            else:
                self.pixmap.draw_line(gc, x, 0, x, height)
                self.pixmap.draw_layout(gc, x + 2, 0, layout)

        for y in range(height):
            if y % ygrid == 0:
                self.pixmap.draw_line(gc, 0, y, width, y)

        return True


    def expose_event(self, widget, event):
        # Redraw the screen from the backing pixmap
        x , y, width, height = event.area
        widget.window.draw_drawable(widget.get_style().fg_gc[gtk.STATE_NORMAL],
                                    self.pixmap, x, y, x, y, width, height)

        return False


    def redraw_input(self, widget):
        # redraw the pixmap
        self.configure_event(widget, None)

        # force the drawing area to be redrawn
        alloc = widget.get_allocation()
        rect = gtk.gdk.Rectangle(alloc.x, alloc.y, alloc.width, alloc.height)
        widget.window.invalidate_rect(rect, True)


    def draw_brush(self, widget, x, y):
        # Draw a rectangle on the screen
        rect = (int(x-5), int(y-5), 10, 10)
        self.pixmap.draw_rectangle(widget.get_style().black_gc, True,
                                   rect[0], rect[1], rect[2], rect[3])
        widget.queue_draw_area(rect[0], rect[1], rect[2], rect[3])


    def button_press_event(self, widget, event):
        if event.button == 1 and self.pixmap != None:
            pass#self.draw_brush(widget, event.x, event.y)
        return True


    def motion_notify_event(self, widget, event):
        if event.is_hint:
            x, y, state = event.window.get_pointer()
        else:
            x = event.x
            y = event.y
            state = event.state
        
        if state & gtk.gdk.BUTTON1_MASK and self.pixmap != None:
            width, height = widget.window.get_size()

            freq = (x/float(width))*(self.freq_max - self.freq_min) + self.freq_min
            if freq > self.freq_max:
                freq = self.freq_max
            if freq < self.freq_min:
                freq = self.freq_min

            vol = (height - y)/float(height)
            if vol > 1:
                vol = 1
            if vol < 0:
                vol = 0
            
            vol = 9*vol + 1 # scale to the range 1 - 10
            vol = math.log10(vol) # log scale

            self.set_tone(freq, vol)
      
        return True
        #return widget.emit("motion_notify_event", event)


    def make_menu(self):
        menu_def = """
        <ui>
          <menubar name="MenuBar">
            <menu action="File">
              <menuitem action="SaveAs"/>
              <separator/>
              <menuitem action="Quit"/>
            </menu>
            <menu action="Help">
              <menuitem action="About"/>
            </menu>
          </menubar>
          <toolbar name="ToolBar">
            <toolitem action="Play"/>
            <toolitem action="Stop"/>
          </toolbar>
        </ui>
        """

        def stop(w):
            self.threads['playback'].paused = True

        def play(w):
            self.threads['playback'].paused = False

        # so this runs on older GTK versions (2.2?)
        try:
            self.about_dialog = gtk.AboutDialog()
            self.about_dialog.set_name(NAME)
            self.about_dialog.set_authors(["nbm_clan@yahoo.com"])
            self.about_dialog.set_comments("A software simulation of a Theremin (see http://en.wikipedia.org/wiki/Theremin) with a few added features.")
            self.about_dialog.set_version(VERSION)
            self.about_dialog.set_license("GPLv2")
            self.about_dialog.set_logo(gtk.gdk.pixbuf_new_from_inline(len(self.logo), self.logo, False))
            about_icon = gtk.STOCK_ABOUT
            play_icon = gtk.STOCK_MEDIA_PLAY
            stop_icon = gtk.STOCK_MEDIA_STOP
        except AttributeError, e:
            self.about_dialog = None
            about_icon = None
            play_icon = None
            stop_icon = None


        actions = [
        ('File', None, '_File'),
        ('SaveAs', gtk.STOCK_SAVE_AS, 'Save Recording _As...', None, 'Save recording', self.saveas),
        ('Quit', gtk.STOCK_QUIT, '_Quit', None, 'Quit', self.destroy),
        ('Help', None, '_Help'),
        ('About', about_icon, '_About', None, 'About', lambda w: self.about_dialog and self.about_dialog.show_all() and self.about_dialog.run()),
        
        ('Play', play_icon, 'Play', None, 'Play', play),
        ('Stop', stop_icon, 'Stop', None, 'Stop', stop),
        ]

        ag = gtk.ActionGroup('menu')
        ag.add_actions(actions)
        
        ui = gtk.UIManager()
        ui.insert_action_group(ag, 0)
        ui.add_ui_from_string(menu_def)

        return ui.get_widget('/MenuBar'), ui.get_widget('/ToolBar')


    def make_input_widget(self, lower, upper):
        input_frame = gtk.Frame("Control")
        input = gtk.DrawingArea()
        input.set_size_request(100, 100)

        input.show()

        # Signals used to handle backing pixmap
        input.connect("expose_event", self.expose_event)
        input.connect("configure_event", self.configure_event)

        # Event signals
        input.connect("button_press_event", self.button_press_event)

        input.set_events(gtk.gdk.EXPOSURE_MASK
                                | gtk.gdk.LEAVE_NOTIFY_MASK
                                | gtk.gdk.BUTTON_PRESS_MASK
                                | gtk.gdk.POINTER_MOTION_MASK)
                                #| gtk.gdk.POINTER_MOTION_HINT_MASK)


        input_table = gtk.Table(4, 3, False)
        input_table.attach(input, 2, 3, 2, 3, gtk.EXPAND | gtk.FILL, gtk.FILL, 0, 0)
        
        def motion_notify(ruler, event):
            return ruler.emit("motion_notify_event", event)

        hrule = gtk.HRuler()
        hrule.set_range(lower, upper, lower, upper)

        input.connect_object("motion_notify_event", motion_notify, hrule)
        input_table.attach(hrule, 2, 3, 1, 2, gtk.EXPAND | gtk.SHRINK | gtk.FILL, gtk.FILL, 0, 0)

        vrule = gtk.VRuler()
        vrule.set_range(1, 0, 0, 1)
        input.connect_object("motion_notify_event", motion_notify, vrule)
        input_table.attach(vrule, 1, 2, 2, 3, gtk.FILL, gtk.EXPAND | gtk.SHRINK | gtk.FILL, 0, 0)
        input.connect("motion_notify_event", self.motion_notify_event)

        input_table.attach(gtk.Label("V\no\nl\nu\nm\ne"), 0, 1, 0, 3, gtk.FILL, gtk.EXPAND | gtk.SHRINK | gtk.FILL, 0, 0)
        input_table.attach(gtk.Label("Frequency (Hz)"), 1, 3, 0, 1, gtk.EXPAND | gtk.SHRINK | gtk.FILL, gtk.FILL, 0, 0)

        input_frame.add(input_table)

        return input_frame


    def init_ui(self):
        """All the gory details of the GUI."""

        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_size_request(600, 600)
        self.window.set_title(NAME)
        self.window.set_icon(gtk.gdk.pixbuf_new_from_inline(len(self.logo), self.logo, False))
        
        # so the close button works
        self.window.connect("delete_event", self.delete_event)
        self.window.connect("destroy", self.destroy)

        self.root = gtk.VBox(False, 1)
        self.window.add(self.root)
        self.root.show()

        menubar, toolbar = self.make_menu()
        self.root.pack_start(menubar, False)
        self.root.pack_start(toolbar, False)
        
        opts_box = gtk.HBox(False, 1)
        opts_frame = gtk.Frame("Options")
        opts_frame.add(opts_box)

        self.root.pack_start(opts_frame, False, False)

        mode_and_key = gtk.VBox(False, 1)
        
        mode_frame = gtk.Frame("Output mode")
        mode_frame.set_shadow_type(gtk.SHADOW_NONE)
        mode_ctls = gtk.VBox(False, 1)
        mode_frame.add(mode_ctls)
        mode_and_key.pack_start(mode_frame, False, False)
        opts_box.pack_start(mode_and_key, False, False)

        rb1 = gtk.RadioButton(None, 'continuous')
        rb1.connect("toggled", self.mode_changed, 'continuous')
        mode_ctls.pack_start(rb1, False, False)
        rb2 = gtk.RadioButton(rb1, 'discrete')
        rb2.connect("toggled", self.mode_changed, 'discrete')
        mode_ctls.pack_start(rb2, False, False)

        scale_frame = gtk.Frame("Scale")
        scale_frame.set_shadow_type(gtk.SHADOW_NONE)
        scale_ctls = gtk.VBox(False, 1)
        scale_frame.add(scale_ctls)
        opts_box.pack_start(scale_frame, False, False)

        first_rb = None
        for scale in SCALES:
            rb = gtk.RadioButton(first_rb, scale)
            rb.connect("toggled", self.scale_changed, scale)
            if first_rb == None:
                first_rb = rb
                rb.set_active(True)

            scale_ctls.pack_start(rb, False, False)

        key_frame = gtk.Frame("Key")
        key_frame.set_shadow_type(gtk.SHADOW_NONE)
        key_ctl = gtk.combo_box_new_text()
        for key in ["A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#"]:
            key_ctl.append_text(key)
        key_ctl.set_active(3)
        key_ctl.connect("changed", self.key_changed, key_ctl)
        key_frame.add(key_ctl)
        mode_and_key.pack_start(key_frame, False, False)

        volume_frame = gtk.Frame("Volume")
        volume_frame.set_shadow_type(gtk.SHADOW_NONE)
        volume = gtk.VScale(gtk.Adjustment(value=7, lower=1, upper=10))
        volume.set_draw_value(False)
        volume_frame.add(volume)
        volume.set_inverted(True)
        opts_box.pack_start(volume_frame, False, False)

        volume.connect("value-changed", self.master_volume_changed)
        
        self.root.pack_start(gtk.HSeparator(), False, False)

        self.pixmap = None

        self.inputs = []
        self.inputs.append(self.make_input_widget(self.freq_min, self.freq_max))
        self.root.pack_start(self.inputs[0], True, True)

        self.window.show_all()

        # status
        self.status = gtk.Statusbar()
        self.status.show()
        self.root.pack_end(self.status, False, False)


    def saveas(self, w):
        open_diag = gtk.FileChooserDialog(title="Save Recording", parent=self.window, action=gtk.FILE_CHOOSER_ACTION_SAVE,
                                          buttons=(gtk.STOCK_CANCEL,gtk.RESPONSE_CANCEL,gtk.STOCK_SAVE,gtk.RESPONSE_OK))
        ffilt = gtk.FileFilter()
        ffilt.add_pattern("*.wav")

        open_diag.add_filter(ffilt)
        response = open_diag.run()

        if response == gtk.RESPONSE_OK:
            output = wave.open(open_diag.get_filename(), 'w')
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44100)

            pbar = gtk.ProgressBar()
            pbar.set_fraction(0)

            d = gtk.Dialog(title="Saving recording . . .", parent=self.window,
                           flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                           buttons=(gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT))
            d.action_area.pack_start(pbar, True, True, 0)
            d.set_has_separator(True)
            d.show_all()

            abort = [False]

            def print_response(w, r):
                if abort[0] == False:
                    abort[0] = True

            d.connect("response", print_response)

            n = len(self.threads['playback'].get_wav_data())
            for i,sample in enumerate(self.threads['playback'].get_wav_data()):
                if i % 256 == 0:
                    pbar.set_fraction(float(i)/n)

                    # so that the progress bar dialog shows/updates
                    while gtk.events_pending():
                        gtk.mainiteration()

                    if abort[0]:
                        break

                output.writeframes(struct.pack('h', sample))

            output.close()

            d.destroy()

        open_diag.destroy()



    def new_tone_filter(self):
        self.root_notes = [n for i,n in enumerate(self.shifted_notes) if i % 12 == 0]

        if self.scale == 'chromatic':
            key_notes = NOTES

        elif self.scale == 'diatonic major':
            key_notes = [n for i,n in enumerate(self.shifted_notes) if i % 12 in diatonic_major_intervals]

        elif self.scale == 'pentatonic major':
            key_notes = [n for i,n in enumerate(self.shifted_notes) if i % 12 in pentatonic_major_intervals]

        elif self.scale == 'pentatonic minor':
            key_notes = [n for i,n in enumerate(self.shifted_notes) if i % 12 in pentatonic_minor_intervals]

        elif self.scale == 'blues':
            key_notes = [n for i,n in enumerate(self.shifted_notes) if i % 12 in blues_intervals]

        self.tone_filter = discrete_tones(just_freqs(key_notes))
        self.discrete_notes = key_notes

        for input in self.inputs:
            self.redraw_input(input)


    def scale_changed(self, button, scale_name):
        if button.get_active():
            self.scale = scale_name
            self.new_tone_filter()
    
    
    def mode_changed(self, button, mode):
        if button.get_active():
            self.mode = mode
            self.new_tone_filter()


    def key_changed(self, button, key):
        self.key = key.get_active_text()

        self.shifted_notes = list(NOTES)
        shifts = {
            'A': 9,
            'A#': 10,
            'B': 11,
            'C': 0,
            'C#': 1,
            'D': 2,
            'D#': 3,
            'E': 4,
            'F': 5,
            'F#': 6,
            'G': 7,
            'G#': 8,
        }

        for i in range(shifts[self.key]):
            self.shifted_notes.append(self.shifted_notes.pop(0))

        self.new_tone_filter()


    def master_volume_changed(self, slider):
        self.master_volume = math.log10(slider.get_value())
        self.set_tone(self.freq, self.vol)


    def set_tone(self, freq, vol):
        self.freq = freq
        self.vol = vol

        if self.mode == 'discrete':
            closest = self.tone_filter(freq)
        else:
            closest = freq

        self.status.push(self.status.get_context_id("note"), "Output frequency:  %.2f Hz - volume %.2f%%" % (closest, vol))

        self.threads['playback'].set_new_freq(closest, vol*self.master_volume)


    def pause(self, button):
        if button.get_active():
            self.threads['playback'].paused = False
        else:
            self.threads['playback'].paused = True
    
    
    def __init__(self, device):

        self.threads = {}

        self.threads['playback'] = PlaybackThread("playback", device)

        self.freq = INIT_FREQ
        self.freq = 0
        self.freq_max = 2000
        self.freq_min = 20

        self.mode = 'continuous'
        self.scale = 'chromatic'
        self.key = 'C'
        self.shifted_notes = NOTES
        self.discrete_notes = NOTES
        self.root_notes = [x for i,x in enumerate(NOTES) if i % 12 == 0]
        self.master_volume = math.log10(7.2)
        self.vol = 0

        self.tone_filter = discrete_tones(just_freqs(NOTES))

        self.init_ui()
        gtk.gdk.threads_init()

        for thread in self.threads.values():
            thread.start()


    def main(self):
        gtk.gdk.threads_enter()
        gtk.main()
        gtk.gdk.threads_leave()


    logo = "" +\
      "GdkP" +\
      "\0\0$\30" +\
      "\1\1\0\2" +\
      "\0\0\0\300" +\
      "\0\0\0""0" +\
      "\0\0\0""0" +\
      "\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376\0\334\334\334" +\
      "\377uuu\377AAA\377>>>\377\77\77\77\377CCC\377\247\247\247\377\371\371" +\
      "\371\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376\0\360\360" +\
      "\360\0\223\223\223\377bbb\377:::\377333\377///\377///\377===\377\317" +\
      "\317\317\377\376\376\376\0\377\377\377\0\377\377\377\0\377\377\377\0" +\
      "\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\375\375\375" +\
      "\0\376\376\376\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376" +\
      "\0\353\353\353\252\335\335\335\377\322\322\322\377\276\276\276\377\225" +\
      "\225\225\377GGG\377---\377///\377\204\204\204\377\375\375\375\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\362\362\362\0\376\376\376\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\372\372\372\0\340\340\340\377\343\343\343\377\335" +\
      "\335\335\377\325\325\325\377\241\241\241\377```\377---\377)))\377```" +\
      "\377\375\375\375\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\376\376\376\0\355\355\355f\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\362\362\362\0\337\337" +\
      "\337\377\340\340\340\377\331\331\331\377\310\310\310\377\200\200\200" +\
      "\377999\377)))\377***\377QQQ\377\373\373\373\0\376\376\376\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376" +\
      "\376\0\356\356\356D\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\356\356\356D\262\262\262\377\277\277\277\377}}}\377\226\226\226" +\
      "\377NNN\377AAA\377)))\377+++\377iii\377\374\374\374\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376" +\
      "\376\376\0\360\360\360\0\376\376\376\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\375\375\375\0\375\375\375\0\375\375\375\0\371" +\
      "\371\371\0\374\374\374\0\374\374\374\0\376\376\376\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\372\372\372\0\326\326\326\377\211\211\211\377\306\306\306" +\
      "\377\301\301\301\377\205\205\205\377FFF\377;;;\377rrr\377\302\302\302" +\
      "\377\375\375\375\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\376\376\376\0\360\360\360\0\376\376" +\
      "\376\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\375\375" +\
      "\375\0\361\361\361\0\331\331\331\377\227\227\227\377\222\222\222\377" +\
      "\335\335\335\377\375\375\375\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\374\374\374" +\
      "\0\314\314\314\377ccc\377\303\303\303\377\315\315\315\377vvv\377AAA\377" +\
      "WWW\377\213\213\213\377\365\365\365\0\376\376\376\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376" +\
      "\376\376\0\357\357\357\"\376\376\376\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\376\376\376\0\311\311\311\377bbb\377@@@\377" +\
      ">>>\377\254\254\254\377\375\375\375\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\375" +\
      "\375\375\0\314\314\314\377}}}\377\271\271\271\377\257\257\257\377ZZZ" +\
      "\377DDD\377ddd\377\335\335\335\377\375\375\375\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\375\375\375\0\355\355\355f\376\376\376\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\375\375\375\0\334\334\334\377\215" +\
      "\215\215\377\214\214\214\377\202\202\202\377\232\232\232\377\370\370" +\
      "\370\0\376\376\376\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\375\375\375\0\325\325\325\377\237" +\
      "\237\237\377\235\235\235\377\211\211\211\377WWW\377DDD\377\213\213\213" +\
      "\377\373\373\373\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\375\375" +\
      "\375\0\355\355\355f\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\375\375\375\0\322\322\322\377\261\261\261\377\315" +\
      "\315\315\377\271\271\271\377www\377\311\311\311\377\376\376\376\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\376\376\376\0\352\352\352\314\320\320\320\377\260\260\260" +\
      "\377ccc\377AAA\377FFF\377\237\237\237\377\371\371\371\0\375\375\375\0" +\
      "\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\376\376\376\0\355\355\355f\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376" +\
      "\0\323\323\323\377\307\307\307\377\326\326\326\377\317\317\317\377\237" +\
      "\237\237\377\354\354\354\210\376\376\376\0\376\376\376\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376" +\
      "\0\367\367\367\0\312\312\312\377\211\211\211\377YYY\377XXX\377fff\377" +\
      "\241\241\241\377\322\322\322\377\375\375\375\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\376\376\376\0\355\355\355f\376\376\376\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\376\376\376\0\375\375\375\0\337\337\337\377\322" +\
      "\322\322\377\314\314\314\377\210\210\210\377\265\265\265\377\374\374" +\
      "\374\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376\0\376\376" +\
      "\376\0\376\376\376\0\377\377\377\0\375\375\375\0\371\371\371\0\277\277" +\
      "\277\377\324\324\324\377\245\245\245\377\251\251\251\377\314\314\314" +\
      "\377rrr\377\77\77\77\377\322\322\322\377\375\375\375\0\375\375\375\0" +\
      "\376\376\376\0\376\376\376\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\376\376\376\0\355\355\355f\376\376\376\0\377\377\377\0\377\377\377" +\
      "\0\375\375\375\0\374\374\374\0\361\361\361\0\320\320\320\377\243\243" +\
      "\243\377fff\377TTT\377\355\355\355f\374\374\374\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\374\374\374\0\370\370\370\0\347\347\347\377\331" +\
      "\331\331\377\334\334\334\377\223\223\223\377zzz\377\343\343\343\377\337" +\
      "\337\337\377\277\277\277\377OOO\377>>>\377'''\377888\377\211\211\211" +\
      "\377\343\343\343\377\374\374\374\0\375\375\375\0\376\376\376\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\375\375\375\0\356\356\356D\376\376\376\0\377\377" +\
      "\377\0\377\377\377\0\375\375\375\0\351\351\351\356\272\272\272\377\303" +\
      "\303\303\377\276\276\276\377ooo\377666\377\230\230\230\377\372\372\372" +\
      "\0\376\376\376\0\376\376\376\0\376\376\376\0\377\377\377\0\377\377\377" +\
      "\0\376\376\376\0\376\376\376\0\375\375\375\0\264\264\264\377MMM\3772" +\
      "22\377+++\377666\377111\377777\377___\377\203\203\203\377UUU\377\227" +\
      "\227\227\377KKK\377%%%\377'''\377(((\377888\377|||\377\267\267\267\377" +\
      "\361\361\361\0\376\376\376\0\376\376\376\0\376\376\376\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\376\376\376\0\375\375\375\0\356\356\356" +\
      "D\377\377\377\0\377\377\377\0\377\377\377\0\375\375\375\0\365\365\365" +\
      "\0yyy\377III\377EEE\377###\377!!!\377111\377\320\320\320\377\372\372" +\
      "\372\0\374\374\374\0\375\375\375\0\374\374\374\0\371\371\371\0\373\373" +\
      "\373\0\345\345\345\377\235\235\235\377---\377&&&\377\"\"\"\377///\377" +\
      "333\377555\377---\377eee\377\207\207\207\377HHH\377\233\233\233\377'" +\
      "''\377$$$\377%%%\377###\377###\377$$$\377'''\377\77\77\77\377\247\247" +\
      "\247\377\373\373\373\0\374\374\374\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\376\376\376\0\375\375\375\0\357\357\357\"\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\376\376\376\0\374\374\374\0\354\354\354\210" +\
      "VVV\377!!!\377\40\40\40\377\37\37\37\377!!!\377555\377\255\255\255\377" +\
      "\340\340\340\377\304\304\304\377\301\301\301\377\202\202\202\377vvv\377" +\
      "BBB\377+++\377%%%\377\"\"\"\377\37\37\37\377...\377///\377///\377jjj" +\
      "\377\336\336\336\377\342\342\342\377\321\321\321\377ggg\377$$$\377!!" +\
      "!\377$$$\377'''\377\40\40\40\377\37\37\37\377!!!\377%%%\377,,,\377\311" +\
      "\311\311\377\374\374\374\0\376\376\376\0\377\377\377\0\377\377\377\0" +\
      "\377\377\377\0\376\376\376\0\361\361\361\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\374\374\374\0\336\336\336" +\
      "\377===\377!!!\377\36\36\36\377\36\36\36\377\40\40\40\377\"\"\"\377*" +\
      "**\377&&&\377&&&\377&&&\377$$$\377&&&\377'''\377!!!\377\36\36\36\377" +\
      "\36\36\36\377...\377)))\377(((\377\201\201\201\377\342\342\342\377\343" +\
      "\343\343\377\314\314\314\377777\377###\377\36\36\36\377%%%\377\35\35" +\
      "\35\377\36\36\36\377\36\36\36\377\37\37\37\377$$$\377%%%\377WWW\377\361" +\
      "\361\361\0\375\375\375\0\375\375\375\0\376\376\376\0\377\377\377\0\376" +\
      "\376\376\0\361\361\361\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\376\376\376\0\375\375\375\0\307\307\307\377" +\
      "...\377\37\37\37\377\36\36\36\377\37\37\37\377\35\35\35\377!!!\377\40" +\
      "\40\40\377\37\37\37\377!!!\377!!!\377\"\"\"\377$$$\377!!!\377\35\35\35" +\
      "\377\34\34\34\377---\377###\377,,,\377\203\203\203\377\341\341\341\377" +\
      "\342\342\342\377\220\220\220\377&&&\377\37\37\37\377\36\36\36\377###" +\
      "\377\35\35\35\377\34\34\34\377\34\34\34\377\37\37\37\377$$$\377$$$\377" +\
      "$$$\377hhh\377\350\350\350\377\374\374\374\0\377\377\377\0\377\377\377" +\
      "\0\375\375\375\0\360\360\360\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\375\375\375" +\
      "\0\251\251\251\377+++\377\40\40\40\377!!!\377\36\36\36\377\36\36\36\377" +\
      "\40\40\40\377\36\36\36\377\34\34\34\377\36\36\36\377!!!\377\"\"\"\377" +\
      "!!!\377\35\35\35\377\33\33\33\377,,,\377$$$\377000\377\177\177\177\377" +\
      "\341\341\341\377\341\341\341\377JJJ\377\"\"\"\377\37\37\37\377(((\377" +\
      "\37\37\37\377\33\33\33\377\34\34\34\377\34\34\34\377\35\35\35\377\40" +\
      "\40\40\377!!!\377!!!\377\37\37\37\377JJJ\377\352\352\352\377\375\375" +\
      "\375\0\377\377\377\0\374\374\374\0\354\354\354\210\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\375\375\375\0\372\372\372\0\267\267\267\377999\377\"\"\"\377" +\
      "\37\37\37\377\40\40\40\377\"\"\"\377\36\36\36\377\40\40\40\377\"\"\"" +\
      "\377!!!\377%%%\377!!!\377\36\36\36\377\33\33\33\377(((\377$$$\377666" +\
      "\377vvv\377\342\342\342\377\266\266\266\377---\377\40\40\40\377$$$\377" +\
      "xxx\377\200\200\200\377ggg\377\35\35\35\377\35\35\35\377\34\34\34\377" +\
      "!!!\377\"\"\"\377\36\36\36\377\35\35\35\377\35\35\35\377jjj\377\346\346" +\
      "\346\377\377\377\377\0\375\375\375\0\353\353\353\252\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\375\375\375\0\343\343\343\377" +\
      "\233\233\233\377KKK\377///\377LLL\377jjj\377\205\205\205\377\222\222" +\
      "\222\377\233\233\233\377\240\240\240\377999\377!!!\377\35\35\35\377'" +\
      "''\377$$$\377:::\377lll\377\342\342\342\377xxx\377%%%\377!!!\377---\377" +\
      "ggg\377hhh\377KKK\377\34\34\34\377\32\32\32\377\33\33\33\377###\377\40" +\
      "\40\40\377***\377***\377\"\"\"\377\37\37\37\377777\377\377\377\377\0" +\
      "\376\376\376\0\353\353\353\252\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\376\376\376\0\375\375\375\0\374\374\374\0\366\366\366" +\
      "\0\362\362\362\0\373\373\373\0\375\375\375\0\373\373\373\0\374\374\374" +\
      "\0\374\374\374\0\373\373\373\0nnn\377!!!\377\35\35\35\377%%%\377)))\377" +\
      "999\377aaa\377\324\324\324\377\77\77\77\377!!!\377(((\377$$$\377\35\35" +\
      "\35\377\34\34\34\377\34\34\34\377\31\31\31\377\33\33\33\377\32\32\32" +\
      "\377\40\40\40\377:::\377\257\257\257\377\267\267\267\377sss\377LLL\377" +\
      "$$$\377\377\377\377\0\375\375\375\0\354\354\354\210\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\376" +\
      "\376\376\0\377\377\377\0\376\376\376\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\376\376\376\0\244\244\244\377" +\
      "$$$\377\37\37\37\377!!!\377000\377555\377LLL\377\240\240\240\377...\377" +\
      "###\377'''\377\34\34\34\377\32\32\32\377\32\32\32\377\32\32\32\377\33" +\
      "\33\33\377\34\34\34\377$$$\377\276\276\276\377\237\237\237\377\272\272" +\
      "\272\377\236\236\236\377bbb\377\240\240\240\377LLL\377\377\377\377\0" +\
      "\374\374\374\0\356\356\356D\377\377\377\0\377\377\377\0\377\377\377\0" +\
      "\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\272\272\272\377&&&\377\37\37\37\377\36" +\
      "\36\36\377666\377000\377222\377TTT\377---\377&&&\377\33\33\33\377\33" +\
      "\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\34\34\34\377\35\35" +\
      "\35\377WWW\377\367\367\367\0\326\326\326\377\222\222\222\377fff\377K" +\
      "KK\377mmm\377BBB\377\376\376\376\0\374\374\374\0\356\356\356D\376\376" +\
      "\376\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\310\310" +\
      "\310\377(((\377\36\36\36\377\34\34\34\377***\377+++\377!!!\377777\377" +\
      "%%%\377\32\32\32\377\31\31\31\377\32\32\32\377\32\32\32\377\32\32\32" +\
      "\377\32\32\32\377\33\33\33\377\37\37\37\377\230\230\230\377\370\370\370" +\
      "\0\202\202\202\377\232\232\232\377ccc\377>>>\377)))\377kkk\377\376\376" +\
      "\376\0\364\364\364\0\325\325\325\377\374\374\374\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\376\376\376\0\323\323\323\377,,,\377\40\40\40" +\
      "\377\35\35\35\377\37\37\37\377///\377,,,\377222\377\32\32\32\377\31\31" +\
      "\31\377\31\31\31\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32" +\
      "\377\36\36\36\377&&&\377\333\333\333\377\375\375\375\0\374\374\374\0" +\
      "\364\364\364\0\251\251\251\377uuu\377DDD\377\215\215\215\377\376\376" +\
      "\376\0\335\335\335\377\214\214\214\377\372\372\372\0\377\377\377\0\376" +\
      "\376\376\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\376\376\376\0\337\337\337\377000\377\"\"\"\377" +\
      "\37\37\37\377\37\37\37\377'''\377...\377\34\34\34\377\31\31\31\377\32" +\
      "\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\33\33" +\
      "\33\377\37\37\37\377bbb\377\373\373\373\0\376\376\376\0\376\376\376\0" +\
      "\374\374\374\0\361\361\361\0\371\371\371\0\307\307\307\377vvv\377\375" +\
      "\375\375\0\320\320\320\377xxx\377\353\353\353\252\377\377\377\0\377\377" +\
      "\377\0\376\376\376\0\376\376\376\0\375\375\375\0\376\376\376\0\375\375" +\
      "\375\0\376\376\376\0\376\376\376\0\376\376\376\0\376\376\376\0\375\375" +\
      "\375\0\376\376\376\0\376\376\376\0\376\376\376\0\376\376\376\0\376\376" +\
      "\376\0\376\376\376\0\376\376\376\0\353\353\353\252999\377&&&\377###\377" +\
      "!!!\377\37\37\37\377\37\37\37\377\33\33\33\377\32\32\32\377\33\33\33" +\
      "\377\33\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\33\33\33\377" +\
      "\37\37\37\377\235\235\235\377\376\376\376\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\376\376\376\0\374\374\374" +\
      "\0\331\331\331\377\311\311\311\377\236\236\236\377\314\314\314\377\310" +\
      "\310\310\377\305\305\305\377\302\302\302\377\300\300\300\377\300\300" +\
      "\300\377\306\306\306\377\311\311\311\377\305\305\305\377\310\310\310" +\
      "\377\313\313\313\377\321\321\321\377\326\326\326\377\331\331\331\377" +\
      "\333\333\333\377\340\340\340\377\345\345\345\377\347\347\347\377\353" +\
      "\353\353\252\360\360\360\0\341\341\341\377999\377%%%\377###\377\"\"\"" +\
      "\377\37\37\37\377\32\32\32\377\32\32\32\377\32\32\32\377\34\34\34\377" +\
      "\33\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\33\33\33\377\40" +\
      "\40\40\377\313\313\313\377\376\376\376\0\377\377\377\0\377\377\377\0" +\
      "\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\244\244\244\377\226\226\226\377\201\201\201\377qqq\377|||\377{{{\377" +\
      "iii\377mmm\377iii\377bbb\377ccc\377ZZZ\377YYY\377UUU\377XXX\377PPP\377" +\
      "GGG\377;;;\377AAA\377MMM\377TTT\377\\\\\\\377VVV\377NNN\377222\37777" +\
      "7\377\77\77\77\377EEE\377\77\77\77\377\34\34\34\377\32\32\32\377\31\31" +\
      "\31\377\33\33\33\377\33\33\33\377\34\34\34\377\34\34\34\377\35\35\35" +\
      "\377\35\35\35\377+++\377\355\355\355f\374\374\374\0\376\376\376\0\376" +\
      "\376\376\0\376\376\376\0\376\376\376\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0\236\236\236\377www\377UUU\377UUU\377kkk\377sss\377jjj\377" +\
      "bbb\377eee\377kkk\377iii\377BBB\377###\377+++\377aaa\377KKK\377DDD\377" +\
      "BBB\377:::\377888\377444\377;;;\377;;;\377999\377444\377,,,\377555\377" +\
      "HHH\377\\\\\\\377,,,\377333\377<<<\377YYY\377zzz\377\204\204\204\377" +\
      "ttt\377GGG\377///\377HHH\377\313\313\313\377\320\320\320\377\322\322" +\
      "\322\377\344\344\344\377\372\372\372\0\375\375\375\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\202\202\202\377###\377\35\35\35\377\35\35\35" +\
      "\377\36\36\36\377\37\37\37\377\35\35\35\377\33\33\33\377\33\33\33\377" +\
      "\34\34\34\377\33\33\33\377\40\40\40\377&&&\377,,,\377000\377\34\34\34" +\
      "\377\33\33\33\377\36\36\36\377\36\36\36\377\40\40\40\377\40\40\40\377" +\
      "###\377(((\377111\377555\377TTT\377,,,\377\77\77\77\377YYY\377{{{\377" +\
      "sss\377bbb\377@@@\377###\377\36\36\36\377\36\36\36\377\34\34\34\377\35" +\
      "\35\35\377VVV\377\373\373\373\0\376\376\376\0\374\374\374\0\354\354\354" +\
      "\210\333\333\333\377\332\332\332\377\357\357\357\"\373\373\373\0\374" +\
      "\374\374\0^^^\377\40\40\40\377\33\33\33\377\34\34\34\377\33\33\33\377" +\
      "\32\32\32\377\31\31\31\377\32\32\32\377\31\31\31\377\31\31\31\377\27" +\
      "\27\27\377\32\32\32\377\35\35\35\377\35\35\35\377\33\33\33\377\31\31" +\
      "\31\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32" +\
      "\377\32\32\32\377\32\32\32\377\32\32\32\377\33\33\33\377&&&\377%%%\377" +\
      "FFF\377RRR\377222\377\"\"\"\377###\377\37\37\37\377\36\36\36\377\36\36" +\
      "\36\377\33\33\33\377\34\34\34\377\37\37\37\377PPP\377\372\372\372\0\375" +\
      "\375\375\0\376\376\376\0\375\375\375\0\375\375\375\0\372\372\372\0\344" +\
      "\344\344\377\337\337\337\377\374\374\374\0>>>\377\40\40\40\377\34\34" +\
      "\34\377\35\35\35\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32" +\
      "\377\32\32\32\377\32\32\32\377\31\31\31\377\30\30\30\377\33\33\33\377" +\
      "\33\33\33\377\33\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\32\32\32\377\34\34\34\377\33\33\33\377\32\32\32\377\32\32" +\
      "\32\377\32\32\32\377\36\36\36\377///\377WWW\377GGG\377>>>\377...\377" +\
      "###\377&&&\377\"\"\"\377\34\34\34\377\34\34\34\377\34\34\34\377\36\36" +\
      "\36\377>>>\377\366\366\366\0\376\376\376\0\377\377\377\0\377\377\377" +\
      "\0\376\376\376\0\375\375\375\0\374\374\374\0\355\355\355f\351\351\351" +\
      "\356888\377\37\37\37\377\34\34\34\377\34\34\34\377\32\32\32\377\32\32" +\
      "\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\31\31\31" +\
      "\377\32\32\32\377\33\33\33\377\33\33\33\377\33\33\33\377\33\33\33\377" +\
      "\33\33\33\377\33\33\33\377\33\33\33\377\34\34\34\377\34\34\34\377\34" +\
      "\34\34\377\33\33\33\377\33\33\33\377\35\35\35\377!!!\377%%%\377HHH\377" +\
      "TTT\377CCC\377\35\35\35\377\33\33\33\377\33\33\33\377'''\377>>>\3770" +\
      "00\377,,,\377$$$\377+++\377\353\353\353\252\376\376\376\0\376\376\376" +\
      "\0\376\376\376\0\375\375\375\0\375\375\375\0\365\365\365\0\337\337\337" +\
      "\377\362\362\362\0""666\377\37\37\37\377\34\34\34\377\33\33\33\377\32" +\
      "\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32" +\
      "\32\377\32\32\32\377\33\33\33\377\34\34\34\377\32\32\32\377\33\33\33" +\
      "\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377" +\
      "\33\33\33\377\34\34\34\377\34\34\34\377\34\34\34\377\36\36\36\377\"\"" +\
      "\"\377&&&\377JJJ\377LLL\377BBB\377\35\35\35\377\34\34\34\377\34\34\34" +\
      "\377\35\35\35\377\35\35\35\377\35\35\35\377'''\377222\377111\377\277" +\
      "\277\277\377\340\340\340\377\343\343\343\377\345\345\345\377\355\355" +\
      "\355f\353\353\353\252\352\352\352\314\370\370\370\0\375\375\375\0<<<" +\
      "\377\35\35\35\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377" +\
      "\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\33\33\33\377\32\32\32\377\33\33\33\377\33\33\33\377\34\34" +\
      "\34\377\34\34\34\377\33\33\33\377\33\33\33\377\34\34\34\377\34\34\34" +\
      "\377\34\34\34\377\35\35\35\377\36\36\36\377!!!\377///\377FFF\377888\377" +\
      "444\377\36\36\36\377\35\35\35\377\37\37\37\377\35\35\35\377\33\33\33" +\
      "\377\33\33\33\377\35\35\35\377\37\37\37\377$$$\377\325\325\325\377\375" +\
      "\375\375\0\376\376\376\0\376\376\376\0\376\376\376\0\376\376\376\0\377" +\
      "\377\377\0\376\376\376\0\376\376\376\0""777\377\35\35\35\377\32\32\32" +\
      "\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377" +\
      "\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\33\33\33\377\32" +\
      "\32\32\377\32\32\32\377\34\34\34\377\34\34\34\377\34\34\34\377\33\33" +\
      "\33\377\33\33\33\377\34\34\34\377\34\34\34\377\34\34\34\377\33\33\33" +\
      "\377\34\34\34\377!!!\377***\377AAA\377\35\35\35\377\34\34\34\377\34\34" +\
      "\34\377)))\377$$$\377%%%\377\35\35\35\377\33\33\33\377\34\34\34\377\37" +\
      "\37\37\377%%%\377\321\321\321\377\376\376\376\0\376\376\376\0\377\377" +\
      "\377\0\377\377\377\0\376\376\376\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0;;;\377\34\34\34\377\31\31\31\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\31\31" +\
      "\31\377\30\30\30\377\33\33\33\377\31\31\31\377\31\31\31\377\33\33\33" +\
      "\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377" +\
      "\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377!!!\377///\377@@" +\
      "@\377\34\34\34\377\34\34\34\377\34\34\34\377000\377)))\377\"\"\"\377" +\
      "\35\35\35\377\36\36\36\377\36\36\36\377!!!\377(((\377\315\315\315\377" +\
      "\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0<<<\377\35\35\35\377\31\31" +\
      "\31\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32" +\
      "\377\32\32\32\377\32\32\32\377\31\31\31\377\30\30\30\377\31\31\31\377" +\
      "\31\31\31\377\32\32\32\377\32\32\32\377\34\34\34\377\35\35\35\377\34" +\
      "\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34" +\
      "\34\377\34\34\34\377\"\"\"\377)))\377>>>\377\37\37\37\377\36\36\36\377" +\
      "\35\35\35\377777\377000\377&&&\377---\377$$$\377\"\"\"\377%%%\377)))" +\
      "\377\324\324\324\377\376\376\376\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0""999\377" +\
      "\36\36\36\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\31\31\31\377\30\30" +\
      "\30\377\30\30\30\377\31\31\31\377\32\32\32\377\32\32\32\377\33\33\33" +\
      "\377\33\33\33\377\33\33\33\377\33\33\33\377\33\33\33\377\34\34\34\377" +\
      "\35\35\35\377\34\34\34\377\34\34\34\377\"\"\"\377)))\377===\377\37\37" +\
      "\37\377\36\36\36\377000\377@@@\377222\377777\377HHH\377&&&\377\"\"\"" +\
      "\377&&&\377,,,\377\334\334\334\377\376\376\376\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\77\77\77\377\40\40\40\377\34\34\34\377\32\32\32\377\32\32\32\377" +\
      "\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\32\32\32\377\30\30\30\377\32\32\32\377\33\33\33\377\33\33" +\
      "\33\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32" +\
      "\377\33\33\33\377\35\35\35\377\34\34\34\377\33\33\33\377\40\40\40\377" +\
      ",,,\377===\377\"\"\"\377\"\"\"\377\"\"\"\377\35\35\35\377\33\33\33\377" +\
      ";;;\377GGG\377%%%\377\"\"\"\377'''\377///\377\334\334\334\377\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0""888\377\37\37\37\377\33\33\33\377" +\
      "\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\32\32\32\377\32\32\32\377\30\30\30\377\31\31\31\377\31\31" +\
      "\31\377\33\33\33\377\33\33\33\377\34\34\34\377\33\33\33\377\32\32\32" +\
      "\377\32\32\32\377\32\32\32\377\33\33\33\377\33\33\33\377\34\34\34\377" +\
      "\34\34\34\377\"\"\"\377@@@\377;;;\377\40\40\40\377###\377\"\"\"\377\35" +\
      "\35\35\377\35\35\35\377999\377DDD\377$$$\377\"\"\"\377&&&\377...\377" +\
      "\336\336\336\377\376\376\376\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0""999\377\37" +\
      "\37\37\377\33\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32" +\
      "\32\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\30\30\30" +\
      "\377\30\30\30\377\31\31\31\377\33\33\33\377\33\33\33\377\34\34\34\377" +\
      "\34\34\34\377\33\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\34\34\34\377\34\34\34\377!!!\377666\377<<<\377\"\"\"\377#" +\
      "##\377%%%\377!!!\377!!!\377;;;\377FFF\377%%%\377\"\"\"\377&&&\377000" +\
      "\377\340\340\340\377\376\376\376\0\377\377\377\0\377\377\377\0\377\377" +\
      "\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0:::\377" +\
      "\37\37\37\377\33\33\33\377\32\32\32\377\32\32\32\377\32\32\32\377\32" +\
      "\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\33\33\33\377\31\31" +\
      "\31\377\30\30\30\377\31\31\31\377\34\34\34\377\33\33\33\377\33\33\33" +\
      "\377\34\34\34\377\34\34\34\377\34\34\34\377\32\32\32\377\32\32\32\377" +\
      "\32\32\32\377\32\32\32\377\33\33\33\377\37\37\37\377)))\377BBB\377**" +\
      "*\377+++\377(((\377###\377###\377<<<\377===\377\"\"\"\377\37\37\37\377" +\
      "%%%\377===\377\354\354\354\210\376\376\376\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0""555\377\35\35\35\377\32\32\32\377\32\32\32\377\32\32\32\377\32\32" +\
      "\32\377\32\32\32\377\32\32\32\377\32\32\32\377\34\34\34\377\33\33\33" +\
      "\377\31\31\31\377\30\30\30\377\31\31\31\377\34\34\34\377\33\33\33\377" +\
      "\32\32\32\377\32\32\32\377\34\34\34\377\34\34\34\377\34\34\34\377\33" +\
      "\33\33\377\33\33\33\377\33\33\33\377\33\33\33\377\40\40\40\377)))\377" +\
      "\77\77\77\377...\377///\377,,,\377,,,\377///\377444\377\"\"\"\377!!!" +\
      "\377!!!\377&&&\377ddd\377\374\374\374\0\376\376\376\0\377\377\377\0\377" +\
      "\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377" +\
      "\377\377\0""333\377\33\33\33\377\31\31\31\377\32\32\32\377\32\32\32\377" +\
      "\32\32\32\377\32\32\32\377\32\32\32\377\32\32\32\377\34\34\34\377\34" +\
      "\34\34\377\32\32\32\377\31\31\31\377\31\31\31\377\34\34\34\377\32\32" +\
      "\32\377\32\32\32\377\32\32\32\377\34\34\34\377\34\34\34\377\34\34\34" +\
      "\377\34\34\34\377\34\34\34\377\34\34\34\377\34\34\34\377###\377)))\377" +\
      "\77\77\77\377***\377&&&\377***\377...\377+++\377$$$\377\37\37\37\377" +\
      "\40\40\40\377###\377(((\377\177\177\177\377\374\374\374\0\376\376\376" +\
      "\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377\0\377\377\377" +\
      "\0\377\377\377\0\377\377\377\0"


def usage(pname):
    print """Usage:  %s [OPTIONS]

Options:

    --device=DEV    The device filename to open.  Defauts to /dev/dsp.
    --help          Display this help text and exit.
    """ % pname


def main():
    import getopt
    import sys

    opts, args = getopt.getopt(sys.argv[1:], '', ['device=', 'help'])

    dev = '/dev/dsp'
    for opt,val in opts:
        if opt == '--device':
            dev = val
        elif opt == '--help':
            usage(sys.argv[0])
            sys.exit(0)

    app = ThereminApp(device=dev)
    app.main()


if __name__ == '__main__': main()
