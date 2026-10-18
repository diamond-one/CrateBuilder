"""Exercise working-screen changes without opening audio or MIDI devices."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cratebuilder import desk_theme as T
from cratebuilder.model import Assignment, Sample, Session
from cratebuilder.ui import App


class GrungeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, CRATEBUILDER_DATA_DIR=self.temp.name)
        self.environment.start()
        self.app = App()
        self.app.geometry('1440x960+10000+10000')
        samples = [Sample(str(i), f'C:/generated/Kick {i}.wav', f'Kick {i}.wav', 'kick', 1,
            'generated fixture', .2, 44100, 1, str(i), 100, 1,
            {'centroid': 300, 'decay': .1, 'crest': 8, 'flatness': .1, 'waveform': [.1, .2],
             'available': True}) for i in range(12)]
        self.app.session = Session('C:/generated', samples, kit_size=16, target='sp404')
        self.app.session.pads[4] = Assignment('0', 'Original fixture')
        self.app.refresh()
        self.app.workspace_tabs.set('Kit')
        self.app.update()

    def tearDown(self):
        self.app.close_app()
        self.environment.stop()
        self.temp.cleanup()

    def test_gallery_palette_keeps_working_text_readable(self):
        def luminance(color):
            rgb = [int(color[i:i+2], 16)/255 for i in (1, 3, 5)]
            linear = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in rgb]
            return sum(a*b for a,b in zip(linear, (.2126, .7152, .0722)))
        for foreground, background in ((T.TEXT,T.BG), (T.TEXT,T.CONTROL), (T.MUTED,T.PANEL),
            (T.TEXT,T.CREATIVE_PAD), (T.ON_SELECTED,T.SELECTED), (T.ON_ACTION,T.GREEN),
            (T.AMBER,T.PANEL)):
            low,high=sorted((luminance(foreground),luminance(background)))
            self.assertGreaterEqual((high+.05)/(low+.05),4.5, (foreground,background))
        self.assertEqual(T.BG, '#302e2b')
        self.assertEqual(T.SELECTED, '#a9433e')

    def test_generation_pads_and_exports_fit_small_and_large_windows(self):
        for geometry in ('1200x760', '1440x960', '1600x1000'):
            self.app.geometry(geometry+'+10000+10000')
            for target in ('SP-404MKII','MPC1000'):
                self.app.target_var.set(target)
                self.app.target_changed()
                self.app.update()
                for button in self.app.pad_buttons.values():
                    self.assertGreaterEqual(button.winfo_width(),100)
                    self.assertGreaterEqual(button.winfo_height(),50)
                for action in (self.app.shuffle_button,self.app.export_button,self.app.live_button):
                    self.assertTrue(action.winfo_ismapped())
                    self.assertLessEqual(action.winfo_rootx()+action.winfo_width(), self.app.winfo_rootx()+self.app.winfo_width())
                    self.assertLessEqual(action.winfo_rooty()+action.winfo_height(), self.app.winfo_rooty()+self.app.winfo_height())
                # Replacement is in the first viewport, rather than below file details.
                self.assertLess(self.app.keep_sound_button.winfo_rooty()+self.app.keep_sound_button.winfo_height(),
                                self.app.export_button.winfo_rooty())

    def test_trying_and_comparing_preserve_the_pad_until_explicit_keep(self):
        self.app.rank_serial += 1  # Ignore any previous worker's candidate result.
        candidate = self.app.session.samples[1]
        self.app.set_candidates([(1,candidate,'Alternative fixture')])
        original = copy.deepcopy(self.app.session.pads)
        with patch.object(self.app,'audio_job') as audio:
            self.app.preview_candidate()
            self.assertIn('Trying: Kick 1',self.app.candidate_preview_label.cget('text'))
            self.assertEqual(self.app.session.pads,original)
            self.app.audition()
            self.assertIn('Hearing pad: Kick 0',self.app.candidate_preview_label.cget('text'))
            self.assertEqual(self.app.reason_label.cget('text'),'Original fixture')
            self.assertEqual(audio.call_count,2)
        self.app.use_candidate()
        self.assertEqual(self.app.session.pads[4].sample_id,'1')
        self.assertTrue(self.app.session.pads[4].locked)
        self.assertEqual(self.app.session.pads[:4],original[:4])
        self.assertEqual(self.app.session.pads[5:],original[5:])
        self.app.change_bank(1)
        self.app.change_bank(0)
        self.assertEqual(self.app.session.pads[4].sample_id,'1')

    def test_no_candidate_cannot_be_kept_and_8_pad_groups_remain_available(self):
        self.app.set_candidates([])
        self.assertEqual(self.app.keep_sound_button.cget('state'),'disabled')
        self.app.session.kit_size=8
        self.app.refresh()
        self.app.update()
        self.assertTrue(all(bar.winfo_ismapped() for bar in self.app.group_bars.values()))
        self.assertEqual(self.app.kit_for_group(1),1)

    def test_transfer_actions_stay_visible_and_connection_details_start_closed(self):
        self.app.send_to_sp(auto_probe=False)
        window=self.app.transfer_window
        window.geometry('760x660+10000+10000')
        self.app.update()
        self.assertFalse(self.app.transfer_advanced.winfo_ismapped())
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        send=next(w for w in descendants(window) if type(w).__name__=='CTkButton'
                  and w.cget('text')=='Send reviewed kit to SP')
        self.assertTrue(send.winfo_ismapped())
        self.assertEqual(send.cget('state'),'disabled')
        self.assertLessEqual(send.winfo_rooty()+send.winfo_height(),window.winfo_rooty()+window.winfo_height())
        window.destroy()


if __name__=='__main__':
    unittest.main()
