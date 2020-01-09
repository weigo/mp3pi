#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Diese Datei bildet das Hauptprogramm des mp3pi-Projekts.
"""

import os
import sys
import re
import subprocess
import threading
import time
import signal
import select

os.environ['KIVY_NO_FILELOG'] = '1'
from kivy.app import App
from kivy.clock import mainthread, Clock
from kivy.core.window import Window
from kivy.graphics import Color
from kivy.logger import Logger
from kivy.properties import BooleanProperty
from kivy.properties import ObjectProperty
from kivy.uix.behaviors import FocusBehavior
from kivy.uix.button import Button
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.layout import LayoutSelectionBehavior
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.screenmanager import ScreenManager, Screen, NoTransition
from kivy.uix.settings import SettingsWithTabbedPanel

from imageviewer import ImageViewer
from nmcli import nmcli
from radiostations import RadioStations
from screensaver import Rpi_ScreenSaver

from http.server import BaseHTTPRequestHandler, HTTPServer
import markup

RootApp = "init"
ConfigObject = None
ImageViewerObject = None
last_activity_time = 0

audio_interface = "pulse"
#audio_interface = "alsa"
'''Auswahl des Audio-Interface. "pulse" oder "alsa".'''

if audio_interface == "alsa":
  from audio import AlsaInterface

class Station(RecycleDataViewBehavior, Button):
  index = None
  selected = BooleanProperty(False)
  selectable = BooleanProperty(True)

  def __init__(self, **kwargs):
    super(Station, self).__init__(**kwargs)

  def refresh_view_attrs(self, rv, index, data):
    ''' Catch and handle the view changes '''

    self.index = index
    return super(Station, self).refresh_view_attrs(
      rv, index, data)

  def on_touch_down(self, touch):
    ''' Add selection on touch down '''
    if super(Station, self).on_touch_down(touch):
      return True
    if self.collide_point(*touch.pos) and self.selectable:
      return self.parent.select_with_touch(self.index, touch)

  def on_release(self):
    if self.selected:
      self.parent.deselect_node(self.index)
    else:
      self.parent.select_node(self.index)

    super(Station, self).on_release()

  def apply_selection(self, rv, index, is_selected):
    ''' Respond to the selection of items in the view. '''
    self.selected = is_selected
    super(Station, self).apply_selection(rv, index, is_selected)

class StationListView(RecycleView):
  def __init__(self, **kwargs):
    self.data = []
    super(StationListView, self).__init__(**kwargs)

  def scroll_to_index(self, index):
    box = self.children[0]
    pos_index = (box.default_size[1] + box.spacing) * index
    scroll = self.convert_distance_to_scroll(
      0, pos_index - (self.height * 0.5))[1]
    if scroll > 1.0:
      scroll = 1.0
    elif scroll < 0.0:
      scroll = 0.0
    self.scroll_y = 1.0 - scroll

  def convert_distance_to_scroll(self, dx, dy):
    box = self.children[0]
    wheight = box.default_size[1] + box.spacing

    if not self._viewport:
      return 0, 0
    vp = self._viewport
    vp_height = len(self.data) * wheight
    if vp.width > self.width:
      sw = vp.width - self.width
      sx = dx / float(sw)
    else:
      sx = 0
    if vp_height > self.height:
      sh = vp_height - self.height
      sy = dy / float(sh)
    else:
      sy = 1
    return sx, sy

class SelectableRecycleBoxLayout(FocusBehavior, LayoutSelectionBehavior,
                                 RecycleBoxLayout):
  ''' Adds selection and focus behaviour to the view. '''

  def get_nodes(self):
    nodes = self.get_selectable_nodes()
    if self.nodes_order_reversed:
      nodes = nodes[::-1]
    if not nodes:
      return None, None

    selected = self.selected_nodes
    if not selected:  # nothing selected, select the first
      self.select_node(nodes[0])
      return None, None

    if len(nodes) == 1:  # the only selectable node is selected already
      return None, None

    last = nodes.index(selected[-1])
    self.clear_selection()
    return last, nodes

  def select_next(self):
    last, nodes = self.get_nodes()
    if not nodes:
      return

    if last == len(nodes) - 1:
      self.select_node(nodes[0])
    else:
      self.select_node(nodes[last + 1])

  def select_previous(self):
    last, nodes = self.get_nodes()
    if not nodes:
      return

    if not last:
      self.select_node(nodes[-1])
    else:
      self.select_node(nodes[last - 1])

class Mp3PiAppLayout(Screen):
  """Die Kivy-Layoutklasse."""

  global RootApp, ConfigObject, ImageViewerObject, last_activity_time, audio_interface
  
  isPlaying = False

  playerproc_stop = threading.Event()
  playerthread = None

  statusproc_stop = threading.Event()
  statusthread = None
  
  last_selection_index = None
  default_image = None
  
  # References to kv widgets
  imageid = ObjectProperty(None)
  wlanstatus = ObjectProperty(None)
  search_results_list = ObjectProperty(None)
  search_results_list_controller = ObjectProperty(None)
  search_results_slider = ObjectProperty(None)
  infotext = ObjectProperty(None)
  volume_slider = ObjectProperty(None)

  def __init__(self, **kwargs):
    global RootApp, audio_interface
    super(Mp3PiAppLayout, self).__init__(**kwargs)
    
    RootApp = self
    
    self.default_image = self.imageid.source

    try:
      if audio_interface == "alsa":
        vol = Alsa.get_volume("")
      else:
        output = subprocess.check_output(["pulseaudio-ctl", "full-status"])
        vol = int(output.decode('utf-8').split(" ")[0])
    except ValueError:
      pass
    else:
      self.volume_slider.value = vol

    # Set up the draggable scrollbar    
    scrlv = self.search_results_list # The ListView's ScrollView
#    scrls = self.search_results_slider
#    scrlv.bind(scroll_y=partial(self.scroll_slider,scrls))
#    scrls.bind(value=partial(self.scroll_list,scrlv))
    self.search_results_list.layout_manager.bind(selected_nodes=self.change_selection)
    self.start_status_thread()

  def scroll_list(self, scrlv, scrls, value):
    """Scrollen der Stationsliste.
    
    Gebunden an die value-Property des Stationslisten-Sliders in __init__.
    """
    scrlv.scroll_y = value
    scrlv._update_effect_y_bounds() # bug in ScrollView
  
  def scroll_slider(self, scrls, scrlv, value):
    """Scrollen des Stationslisten-Sliders.
    
    Gebunden an die scroll_y-Property der Stationsliste in __init__.
    """
    if value >= 0:
    #this to avoid 'maximum recursion depth exceeded' error
      scrls.value = value

  def change_volume(self, value):
    """Ändern der Lautstärke.
    
    Gebunden an die on_value-Property von volume_slider in mp3pi.kv
    """
    global audio_interface
    vol = int(value)
    if audio_interface == "alsa":
      #os.system("amixer set Master %s%%" % vol)
      #os.system("pactl set-sink-volume  bluez_sink.0C_A6_94_E3_76_DA %s%%" % vol)
      Alsa.set_volume("", vol)
    else:
      os.system("pulseaudio-ctl set %s%%" % (vol))

  def change_selection(self, inst, val):
    """Wechsel der Startionsauswahl.
    
    Gebunden an die on_selection_change-Property von search_results_list in __init__
    """
    global ConfigObject

    selection = -1
    for value in val:
      if value is not None:
        selection = value

    if -1 < selection:
      self.last_selection_index = selection
      station_name = self.search_results_list.data[self.last_selection_index].get('text')
      self.stop_player_thread()
      self.change_image(station_name)
      self.start_player_thread(Stations.getStreamURLbyName(station_name))
      ConfigObject.set('General', 'last_station', station_name)
      ConfigObject.write()
      self.search_results_list.scroll_to_index(self.last_selection_index)
    else:
      self.stop_player_thread()

  def change_image(self, station_name):
    """Wechsel des Stations-Bildes."""
    imageUrl = Stations.getImageUrlByName(station_name) 

    if (imageUrl == ''):
      imageUrl = self.default_image

    Logger.info("Mp3Pi GUI: Loading Image from %s" % (imageUrl))
    self.imageid.source = imageUrl

  def stop_player_thread(self):
    """Beenden des Player-Thread.
    
    Setzt den playerproc_stop-Event und wartet auf Beendigung des
    Player-Thread.
    """
    if self.isPlaying or (self.playerthread is not None and self.playerthread.is_alive()):
      Logger.info("Mp3Pi GUI: stopping player")
      if self.playerthread.is_alive():
        Logger.info("Mp3Pi GUI: notifying player thread")
        self.playerproc_stop.set()
        self.playerthread.join(.3)
      self.isPlaying = False
    else:
      Logger.info("Mp3Pi GUI: player already stopped")

  def start_player_thread(self, streamURL):
    """Starten des Player-Thread."""
    if not self.isPlaying:
      Logger.info("Mp3Pi GUI: starting player " + streamURL)
      self.isPlaying = True
      self.playerthread = threading.Thread(target=self.player_proc, args=(streamURL,))
      self.playerthread.daemon = True
      self.playerthread.start()
    else:
      Logger.info("Mp3Pi GUI: player already started")

  def start_status_thread(self):
    """Starten des Status-Thread."""
    self.statusthread = threading.Thread(target=self.status_proc)
    self.statusthread.daemon = True
    self.statusthread.start()

  @mainthread
  def update_infotext(self, text):
    """Update des infotext-Labels (im Mainthread)."""
    self.infotext.text = text
      
  @mainthread
  def update_wlanstatus_text(self, text):
    """Update des wlanstatus-Text (im Mainthread)."""
    self.wlanstatus.text = text
      
  @mainthread
  def update_wlanstatus_icon(self, connection):
    """Update des wlanstatus-Icons (im Mainthread)."""
    lines = []
    for i in self.wlanstatus.canvas.get_group(None)[1:]:
      if type(i) is Color:
        lines.append(i)
        i.a = 1

    signal = int(connection['SIGNAL']) if connection is not None else 0
    max = 4

    if signal < 50:
      max = 3
    elif signal < 70:
      max = 2
    elif signal < 70:
      max = 1

    for i in lines[0:max]:
      i.a = .5

  @mainthread
  def update_search_results_list(self):
    """Update der search_result_list (im Mainthread)."""
    self.search_results_list.data = map(lambda s: {
      'text': s['name'],
      'selected': False,
      'selectable': True}, Stations.data)
    station_name = ConfigObject.get('General','last_station')
    if station_name is not None:
      index = Stations.getIndexByName(station_name)
      if index is not None:
        self.last_selection_index = index

  def player_proc(self, url):
    """Routine des Player-Thread.
    
    mpg123 wird mit 'url' in einem Subprozess gestartet,
    von diesem ausgegebene StreamTitles werden in das infotext-Label übertragen.

    Der Thread kann durch Setzen des playerproc_stop-Events beendet werden.
    """
    global audio_interface
    args = ["mpg123", "--no-control", "--list", url]
    args.extend(["--output", audio_interface])
    #args.extend(["--buffer", "2048"])
    proc = subprocess.Popen(args, shell=False, stdin=None, stderr=subprocess.PIPE, bufsize=0)
    Logger.info("Player: started pid %s" % proc.pid)

    line = []
    errorText = None
    self.playerproc_stop.clear()
    self.update_infotext('*** Starting ***')

    while not self.playerproc_stop.is_set():
      while (not self.playerproc_stop.is_set()
        and proc is not None
        and select.select([proc.stderr], [], [], .1)[0]):


        # check if mpg123 has died
        if proc.returncode is not None:
          errorText = "*** Player died ***"
          self.playerproc_stop.set()
          break

        char = proc.stderr.read(1)

        if char != b'\n':
          line.append(char)
          continue

        line_joined = str(b"".join(line), "UTF-8")

        if 'Invalid playlist from http_open()' in line_joined:
          errorText = "*** Error opening stream ***"
          self.playerproc_stop.set()
          break
          
        if "ICY-NAME: " in line_joined:
          res = re.search("ICY-NAME: (.*)", line_joined)
          if res is not None:
            self.update_infotext(res.group(1))

        if "ICY-META: StreamTitle=" in line_joined:
          res = re.search("StreamTitle='(.*?)';", line_joined)
          if res is not None:
            self.update_infotext(res.group(1))
          else:
            self.update_infotext('')

        #if "ICY-NAME: " in line_joined:
        #  Logger.debug("ICYTAGS: ICY name found: %s " % line_joined.replace("ICY-NAME: ", ""))
        #if "ICY-URL: " in line_joined:
        #  Logger.debug("ICYTAGS: ICY url found: %s " % line_joined.replace("ICY-URL: ", ""))
        #if "ICY-META: StreamTitle=" in line_joined:
        #  Logger.debug("ICYTAGS: ICY StreamTitle found: %s " % line_joined.replace("ICY-META: StreamTitle=", ""))

        line = []

      time.sleep(.1)

    Logger.info("Player: stopping")
    self.playerproc_stop.clear()

    if errorText is None:
      self.update_infotext('')
    else:
      self.update_infotext(errorText)
      Logger.info("Player: " + errorText)

    if proc is not None:
      Logger.info("Player: killing pid %s" % proc.pid)
      #proc.terminate()
      #proc.kill()
      os.kill(proc.pid, signal.SIGTERM)
      proc = None

  def status_proc(self):
    """Routine des Status-Thread.
    
    Es wird in einer Endlosschleife der Status der Netzwerkverbindung
    abgefragt und angezeigt, evtl. die Playlist geladen und in die
    Stationsliste übertragen, und der Screensaver (de-)aktiviert.
    """
    global last_activity_time, ConfigObject, ImageViewerObject
    
    connection = NMCLI.current_connection() 

    self.statusproc_stop.clear()
    while not self.statusproc_stop.is_set():

      if 0 == int(time.time()) % 5:
        connection = NMCLI.current_connection() 

      nowtime = time.strftime("%H:%M", time.localtime())
      if connection is None: 
        self.update_wlanstatus_text("No network connection\n%s" % (nowtime))
      else:
        self.update_wlanstatus_text("%s %s%%\n%s\n%s" % (
          connection.get('SSID', None),
          connection.get('SIGNAL', None),
          NMCLI.get_ip(),
          nowtime))

      # wlan symbol
      self.update_wlanstatus_icon(connection)
        
      # station list
      if Stations.no_data:
        Logger.info("Status: loading playlist")
        self.update_infotext('*** Loading playlist ***')
        playlist = ConfigObject.get('General', 'playlist')
        Stations.load_playlist(playlist)
        if not Stations.no_data:
          Logger.info("Status: {} entries loaded".format(len(Stations.data)))
          self.update_search_results_list()
        self.update_infotext('')
      
      # screensaver
      timeout = max(30, ConfigObject.getint('General', 'screensaver'))

      if (time.time() - last_activity_time) > timeout:
        if self.manager.current == 'main':
          Logger.info("Status: enabling screensaver")
          #self.manager.current = 'screensaver'
          self.manager.current = 'imageviewer'
          if ImageViewerObject.interval != 0:
            ImageViewerObject.start()
          else:
            ScreenSaver.display_off()
      else:
        if self.manager.current != 'main':
          Logger.info("Status: disabling screensaver")
          if ImageViewerObject.interval != 0:
            ImageViewerObject.stop()
          else:
            ScreenSaver.display_on()
          self.manager.current = 'main'
      
      time.sleep(.5)

    Logger.info("Status: stopping")
    self.statusproc_stop.clear()

  def pause(self):
    """on_release-Callback des Pause/Play-Button; s. mp3pi.kv"""
    if self.isPlaying:
      self.stop_player_thread()
      self.search_results_list_controller.deselect_node(self.last_selection_index)
      self.imageid.source = self.default_image
    else:
      if self.last_selection_index is not None:
        self.search_results_list_controller.select_node(self.last_selection_index)

  def next(self):
    """on_release-Callback des Next-Button; s. mp3pi.kv"""
    self.search_results_list_controller.select_next()

  def prev(self):
    """on_release-Callback des Previous-Button; s. mp3pi.kv"""
    self.search_results_list_controller.select_previous()

  def poweroff(self):
    """on_release-Callback des Poweroff-Button; s. mp3pi.kv"""
    Logger.info("Mp3Pi GUI: poweroff")
    os.system("poweroff")

  def reboot(self):
    """on_release-Callback des Reboot-Button; s. mp3pi.kv"""
    Logger.info("Mp3Pi GUI: reboot")
    os.system("reboot")

  def quit(self):
    """on_release-Callback des Quit-Button; s. mp3pi.kv"""
    Logger.info("Mp3Pi GUI: quit")
    App.get_running_app().stop()


class Mp3PiApp(App):
  """Die Kivy-Applikationsklasse."""
  global last_activity_time, ConfigObject, ImageViewerObject

  def build(self):
    """Kivy build() Override Methode."""
    global last_activity_time, ConfigObject, ImageViewerObject

    self.settings_cls = MySettingsWithTabbedPanel

    def on_motion(self, etype, motionevent):
      global last_activity_time
      last_activity_time = time.time()

    Window.bind(on_motion=on_motion)

    ConfigObject = self.config

    sm = ScreenManager(transition=NoTransition())
    sm.add_widget(Mp3PiAppLayout())
    ImageViewerObject = ImageViewer()
    sm.add_widget(ImageViewerObject)
    return(sm)

  def build_config(self, config):
    """Kivy App.build_config() Override Methode."""
    config.setdefaults('General', {'screensaver': "30"})
    config.setdefaults('General', {'image_turnaround': "30"})
    config.setdefaults('General', {'playlist': "radio.de"})
    config.setdefaults('General', {'last_station': None})

  def build_settings(self, settings):
    """Kivy App.build_settings() Override Methode."""
    settings.register_type('scrolloptions', SettingScrollOptions)
    settings.add_json_panel("General", self.config, data="""
      [
        {"type"   : "numeric",
         "title"  : "Screensaver Timeout",
         "section": "General",
         "key"    : "screensaver"
        },
        {"type"   : "numeric",
         "title"  : "ImageViewer Turnaround",
         "section": "General",
         "key"    : "image_turnaround"
        },
        {"type"   : "options",
         "title"  : "Playlist",
         "section": "General",
         "options": ["radio.de", "custom", "favorites"],
         "key"    : "playlist"
        }
      ]"""
#        {"type"   : "string",
#         "title"  : "String",
#         "section": "General",
#         "key"    : "name"
#        },
    )

  def on_stop(self):
    """Kivy App.on_stop Event."""
    # The Kivy event loop is about to stop, set a stop signal;
    # otherwise the app window will close, but the Python process will
    # keep running until all secondary threads exit.
    global RootApp

    Logger.info("Mp3PiApp: shutting down")
    RootApp.playerproc_stop.set()
    RootApp.statusproc_stop.set()
    ScreenSaver.display_on()


class MySettingsWithTabbedPanel(SettingsWithTabbedPanel):
  """Die Settings-Klasse.
  
  Wird verwendet, um bei Änderung der Playlist-Einstellung das
  no_data-Attribute auf True zu setzen.
  """

  def on_close(self):
    Logger.debug("Mp3PiApp.py: MySettingsWithTabbedPanel.on_close")

  def on_config_change(self, config, section, key, value):
    if key == "playlist" or key == "genre":
      Stations.no_data = True
    elif key == "image_turnaround":
      ImageViewerObject.interval = max(0, int(value))
    Logger.info(
      "Mp3PiApp.py: MySettingsWithTabbedPanel.on_config_change: "
      "{0}, {1}, {2}, {3}".format(config, section, key, value))


class SaverScreen(Screen):
  pass


#class SettingsScreen(Screen):
#  def __init__(self, **kwargs):
#    super(SettingsScreen, self).__init__(**kwargs)
#    networklist = []
##    for net in Network.visible_aps:
##      networklist.append(net['ssid'])
##      if net['ssid'] is Network.ssid:
##        self.ids['wlan_list'].text = net[Network.ssid]
#
##    self.ids['wlan_list'].values = networklist
##    self.ids['wlan_list'].bind(text=self.change_wlan_selection)
#
#  def change_wlan_selection(self, spinner, args):
#    Logger.info("WLAN: user selection %s" % args)
##    Logger.info("WLAN: current WLAN %s" % Network.ssid)
#
##    if args != Network.ssid:
##      Logger.info("WLAN: changing WLAN to %s" % args)
##      Network.activate([args])


class HTTPHandler(BaseHTTPRequestHandler):
  """Der Requesthandler des HTTP-Servers."""

  global RootApp, ConfigObject

  def do_GET(self):
    if self.path != "/":
      print(self.path)
      return

    self.page = markup.page()
    self.page.init(title="RaspPi Radio")

    self.page.p('Time is %s' % time.ctime())
    if RootApp.isPlaying:
      self.page.p('Playing %s' % ConfigObject.get('General','last_station'))
    else:
      self.page.p('not playing')

    isFirstline = True
    self.page.table(border="true")
    for row in RootApp.search_results_list.adapter.data:
      self.page.tr()
      if isFirstline:
        for column in row:
          if type(column) == 'float':
            string1 = str(column)
          elif type(column) == 'str':
            string1 = unicode(column, "utf8")
          else:
            string1 = column
          self.page.th(string1, align="left")
        isFirstline = False
      else:
        for column in row:
          if type(row[column]) == 'float':
            string1 = str(row[column])
          elif type(row[column]) == 'str':
            string1 = unicode(row[column], "utf8")
          else:
            string1 = row[column]
          self.page.td(string1)
      self.page.tr.close()
    self.page.table.close()

    self.send_response(200)
    self.send_header('Content-type', 'text/html')
    self.end_headers()
    self.wfile.write(self.page)
    #self.wfile.write(json.dumps(RootApp.search_results_list.adapter.data, indent=4, separators=('.', ': ')))


def start_httpserver_thread():
  """Starten des HTTP-Servers auf Port 8080 in einem eigenen Thread."""
  httpd = HTTPServer(('', 8080), HTTPHandler)
  httpd_thread = threading.Thread(target=httpd.serve_forever)
  httpd_thread.daemon = True
  httpd_thread.start()

def check_audio_device():
  rc = os.system("pactl list sinks")
  if audio_interface == "alsa":
    if rc == 0:
      print("ALSA configured - Pulseaudio is running")
      rc = 1
    else:
      rc = os.system("aplay -l")
  elif audio_interface != "pulse":
    rc = 1
  if rc != 0:
    print("Audio device not configured correctly")
    sys.exit(1)

def signal_handler(signal, frame):
  """Der Signalhandler.
  
  Gebunden an SIGINT im Hauptprogramm.
  """
  print("Signal {} received".format(signal))
  App.get_running_app().stop()
  sys.exit(0);


if __name__ == "__main__":
  signal.signal(signal.SIGINT, signal_handler)

  NMCLI = nmcli()
  
  check_audio_device()
  if audio_interface == "alsa":
    Alsa = AlsaInterface()

  Stations = RadioStations()

  ScreenSaver = Rpi_ScreenSaver()
  ScreenSaver.display_on()
  
  start_httpserver_thread()

  last_activity_time = time.time()

  Mp3PiApp().run()

