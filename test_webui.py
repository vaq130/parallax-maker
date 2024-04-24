import unittest
from contextvars import copy_context
from dash._callback_context import context_value
from dash._utils import AttributeDict
from webui import filename_add_version, update_threshold_values
from controller import AppState
from pathlib import Path


class TestFilenameAddVersion(unittest.TestCase):
    def test_without_version(self):
        filename = "image.png"
        expected = "image_v2.png"
        self.assertEqual(filename_add_version(filename), expected)

    def test_with_version(self):
        filename = "image_v2.png"
        expected = "image_v3.png"
        self.assertEqual(filename_add_version(filename), expected)
        
    def test_with_directory(self):
        filename = "dir/image.png"
        expected = "dir/image_v2.png"
        self.assertEqual(filename_add_version(filename), expected)

class TestUpdateThresholds(unittest.TestCase):
    def test_update_threshold_values_boundaries(self):
        # Create a dummy state
        state = AppState()
        state.imgThresholds = [0, 10, 20, 30, 40, 255]

        # set up a fake cache
        filename = 'teststate'
        state.cache[filename] = state

        # Call the function
        input_thresholds = [0, 20, 30, 40, 255]
        num_slices = 5
        threshold_values, _, _ = update_threshold_values(input_thresholds, num_slices, filename)

        # Assert that the state is updated
        self.assertEqual(state.imgThresholds, [0, 1, 20, 30, 40, 254, 255])


    def test_update_threshold_values_limit(self):
        # Create a dummy state
        state = AppState()
        state.imgThresholds = [0, 10, 20, 30, 40, 255]

        # set up a fake cache
        filename = 'teststate'
        state.cache[filename] = state

        # Call the function
        input_thresholds = [255, 255, 255, 255, 255]
        num_slices = 5
        threshold_values, _, _ = update_threshold_values(input_thresholds, num_slices, filename)

        # Assert that the state is updated
        self.assertEqual(state.imgThresholds, [0, 255, 256, 257, 258, 254, 255])


if __name__ == '__main__':
    unittest.main()
