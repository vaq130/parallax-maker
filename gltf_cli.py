#!/usr/bin/env python3
# (c) 2024 Niels Provos
#

import argparse
from pathlib import Path
import os
from PIL import Image
import cv2

import numpy as np

from controller import AppState
from webui import export_state_as_gltf
from segmentation import generate_depth_map

def postprocess_depth_map(depth_map, image_alpha):
    depth_map[image_alpha != 255] = 0

    kernel = np.ones((3, 3), np.uint8)

    # erode the alpha channel to remove the feathering
    image_alpha = cv2.erode(image_alpha, kernel, iterations=1)
    depth_map_blur = cv2.blur(depth_map, (15, 15))
    depth_map[image_alpha != 255] = depth_map_blur[image_alpha != 255]

    depth_map = cv2.blur(depth_map, (5, 5))
    depth_map = cv2.dilate(depth_map, kernel, iterations=20)

    # normalize to the smallest value
    smallest_vale = np.quantile(depth_map[image_alpha == 255], 0.01)
    smallest_vale = int(smallest_vale)
    
    # change dtype of depth map to int
    depth_map = depth_map.astype(np.int16)    
    depth_map[:, :] -= smallest_vale
    depth_map = np.clip(depth_map, 0, 255)
    depth_map = depth_map.astype(np.uint8)
    
    return depth_map

def compute_depth_map_for_slices(state: AppState, postprocess: bool = True):
    depth_maps = []
    for i, filename in enumerate(state.image_slices_filenames):
        print(f"Processing {filename}")

        image = state.image_slices[i]

        depth_map = generate_depth_map(image[:, :, :3], model='midas')
        
        if postprocess:      
            image_alpha = image[:, :, 3]
            depth_map = postprocess_depth_map(depth_map, image_alpha)

        depth_image = Image.fromarray(depth_map)

        output_filename = Path(state.filename) / \
            (Path(filename).stem + "_depth.png")

        depth_image.save(output_filename, compress_level=1)
        print(f"Saved depth map to {output_filename}")
        
        depth_maps.append(output_filename)
    return depth_maps

def main():
    os.environ['DISABLE_TELEMETRY'] = 'YES'
    os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'

    # get arguments from the command line
    # -i name of the state file
    # -o output for the gltf file
    parser = argparse.ArgumentParser(
        description='Create a glTF file from the state file')
    parser.add_argument('-i', '--state_file', type=str,
                        help='Path to the state file')
    parser.add_argument('-o', '--output_path', type=str,
                        default='output',
                        help='Path to save the glTF file')
    parser.add_argument('-d', '--depth', action='store_true',
                        help='Compute depth maps for slices')
    parser.add_argument('-s', '--scale', type=float,
                        default=0.0,
                        help='Displacement scale factor')
    args = parser.parse_args()

    state = AppState.from_file(args.state_file)

    output_path = Path(args.output_path)
    if not output_path.exists():
        output_path.mkdir(parents=True)

    if args.depth:
        compute_depth_map_for_slices(state)

    state.max_distance = 100

    gltf_path = export_state_as_gltf(
        state, args.output_path,
        state.camera_distance,
        state.max_distance,
        state.focal_length,
        displacement_scale=args.scale)
    print(f"Exported glTF to {gltf_path}")


if __name__ == '__main__':
    main()
