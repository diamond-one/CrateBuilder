"""Windows output selection stays short without opening any audio hardware."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.preview import (audio_output_label, preferred_audio_output,
                                   simple_audio_outputs)


def output(index, name="OUT (SP-404MKII)", host="Windows WASAPI", rate=48000):
    return {"index": index, "name": name, "hostapi_name": host, "samplerate": rate}


class AudioChoiceTests(unittest.TestCase):
    def test_sp_keeps_its_wasapi_endpoint_and_exact_device_index(self):
        inventory = [output(0, "Microsoft Sound Mapper - Output", "MME"),
                     output(3, host="MME"), output(11, host="Windows DirectSound"),
                     output(15, host="ASIO"), output(20, host="Windows WASAPI"),
                     output(22, host="Windows WDM-KS")]
        result = simple_audio_outputs(inventory)
        self.assertEqual(result, [inventory[4]])
        self.assertEqual(result[0]["index"], 20)
        self.assertEqual(preferred_audio_output(result), 20)

    def test_driver_preference_order_and_fallback_when_wasapi_is_missing(self):
        hosts = ["Windows WDM-KS", "MME", "Windows DirectSound", "ASIO", "Windows WASAPI"]
        inventory = [output(index, host=host) for index, host in enumerate(hosts)]
        for count in range(1, len(inventory) + 1):
            with self.subTest(count=count):
                result = simple_audio_outputs(inventory[:count])
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["index"], count - 1)

    def test_real_speakers_headphones_and_separate_interface_outputs_remain_distinct(self):
        inventory = [output(1, "Speakers (Realtek Audio)", "MME"),
                     output(2, "Headphones (Realtek Audio)", "MME"),
                     output(3, "OUT 1-2 (SP-404MKII)", "Windows WASAPI"),
                     output(4, "OUT 3-4 (SP-404MKII)", "Windows WASAPI"),
                     output(5, "Speakers (Realtek Audio)", "Windows WASAPI"),
                     output(6, "Headphones (Realtek Audio)", "Windows WASAPI")]
        result = simple_audio_outputs(inventory)
        self.assertEqual({device["index"] for device in result}, {3, 4, 5, 6})
        self.assertEqual(len(result), 4)

    def test_case_and_whitespace_variations_match_without_losing_saved_names(self):
        inventory = [output(2, "  out  (sp-404mkii) ", "MME"),
                     output(20, "OUT (SP-404MKII)", "Windows WASAPI")]
        self.assertEqual(simple_audio_outputs(inventory), [inventory[1]])
        self.assertEqual(simple_audio_outputs(inventory)[0]["name"], "OUT (SP-404MKII)")

    def test_two_real_devices_with_same_name_inside_best_host_are_preserved(self):
        inventory = [output(1, "USB Audio", "MME"), output(2, "USB Audio", "MME"),
                     output(20, "USB Audio", "Windows WASAPI"),
                     output(21, "USB Audio", "Windows WASAPI")]
        self.assertEqual([device["index"] for device in simple_audio_outputs(inventory)], [20, 21])

    def test_generic_aliases_are_removed_only_when_real_endpoints_exist(self):
        aliases = [output(0, "Microsoft Sound Mapper - Output", "MME"),
                   output(1, "Primary Sound Driver", "Windows DirectSound"),
                   output(2, "Default Output Device", "Windows WASAPI")]
        self.assertEqual({device["index"] for device in simple_audio_outputs(aliases)}, {0, 1, 2})
        real = output(20)
        self.assertEqual(simple_audio_outputs([*aliases, real]), [real])
        # A real endpoint containing the word 'default' is not a generic alias.
        endpoint = output(30, "Default Speakers (Acme Audio)")
        self.assertEqual(simple_audio_outputs([*aliases, endpoint]), [endpoint])

    def test_unknown_hosts_are_retained_and_empty_inventory_does_not_query_devices(self):
        unknown = output(9, "Studio output", "Custom host")
        with patch("cratebuilder.preview.list_audio_outputs", side_effect=AssertionError("Unexpected hardware query")):
            self.assertEqual(simple_audio_outputs([]), [])
            self.assertEqual(simple_audio_outputs([unknown]), [unknown])

    def test_default_inventory_is_queried_once_and_inputs_are_not_mutated(self):
        inventory = [output(1, host="MME"), output(20)]
        before = deepcopy(inventory)
        with patch("cratebuilder.preview.list_audio_outputs", return_value=inventory) as list_outputs:
            result = simple_audio_outputs()
        list_outputs.assert_called_once_with()
        self.assertEqual(inventory, before)
        result[0]["name"] = "Changed display name"
        self.assertEqual(inventory, before)

    def test_simple_labels_hide_driver_details_while_advanced_labels_show_them(self):
        device = output(20)
        self.assertEqual(audio_output_label(device), "OUT (SP-404MKII)")
        self.assertEqual(audio_output_label(device, advanced=True), "OUT (SP-404MKII) (Windows WASAPI)")

    def test_real_truncated_mme_names_merge_with_their_unique_full_wasapi_names(self):
        inventory = [
            output(5, "VGA (NVIDIA High Definition Aud", "MME"),
            output(6, "Realtek Digital Output (Realtek", "MME"),
            output(8, "DELL S3422DW (NVIDIA High Defin", "MME"),
            output(18, "VGA (NVIDIA High Definition Audio)", "Windows WASAPI"),
            output(19, "Realtek Digital Output (Realtek(R) Audio)", "Windows WASAPI"),
            output(20, "Speakers (SP-404MKII-G)", "Windows WASAPI"),
            output(21, "DELL S3422DW (NVIDIA High Definition Audio)", "Windows WASAPI"),
        ]
        result = simple_audio_outputs(inventory)
        self.assertEqual([device["index"] for device in result], [20, 21, 19, 18])
        self.assertEqual(result[0]["hostapi_name"], "Windows WASAPI")

    def test_ambiguous_mme_prefix_does_not_collapse_distinct_endpoints(self):
        inventory = [
            output(5, "VGA (NVIDIA High Definition Aud", "MME"),
            output(18, "VGA (NVIDIA High Definition Audio A)", "Windows WASAPI"),
            output(19, "VGA (NVIDIA High Definition Audio B)", "Windows WASAPI"),
        ]
        self.assertEqual({device["index"] for device in simple_audio_outputs(inventory)}, {5, 18, 19})

    def test_two_identically_named_real_targets_make_prefix_ambiguous(self):
        inventory = [output(5, "VGA (NVIDIA High Definition Aud", "MME"),
                     output(18, "VGA (NVIDIA High Definition Audio)", "Windows WASAPI"),
                     output(19, "VGA (NVIDIA High Definition Audio)", "Windows WASAPI")]
        self.assertEqual({device["index"] for device in simple_audio_outputs(inventory)}, {5, 18, 19})

    def test_short_names_and_same_or_lower_priority_prefix_targets_stay_distinct(self):
        inventory = [output(5, "Short audio", "MME"),
                     output(18, "Short audio output", "Windows WASAPI"),
                     output(6, "VGA (NVIDIA High Definition Aud", "MME"),
                     output(19, "VGA (NVIDIA High Definition Audio)", "Windows WDM-KS")]
        self.assertEqual({device["index"] for device in simple_audio_outputs(inventory)}, {5, 6, 18, 19})

    def test_full_name_variants_across_drivers_are_resolved_before_prefix_matching(self):
        inventory = [output(5, "VGA  (NVIDIA High Definition Aud", "MME"),
                     output(11, "VGA (NVIDIA High Definition Audio)", "Windows DirectSound"),
                     output(18, "VGA (NVIDIA High Definition Audio)", "Windows WASAPI")]
        self.assertEqual(simple_audio_outputs(inventory), [inventory[2]])

    def test_sp_sorts_first_and_other_friendly_names_sort_case_insensitively(self):
        inventory = [output(2, "Zebra speakers"), output(3, "alpha headphones"),
                     output(20, "Speakers (SP-404MKII-G)"), output(4, "Dell audio")]
        self.assertEqual([device["index"] for device in simple_audio_outputs(inventory)], [20, 3, 4, 2])


if __name__ == "__main__":
    unittest.main()
