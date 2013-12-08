This is copied from this GPL project, and tweaked slightly to work with Python2.7: http://sourceforge.net/projects/ptheremin/

It uses the old-style /dev/dsp OSS audio device that's not used in newer Ubuntu versions. I eventually want to update it to use the whole Alsa/PulseAudio setup, but for now you can install the OSS proxy daemon with the following command:

`sudo apt-get install osspd`
