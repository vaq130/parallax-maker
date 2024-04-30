#!/usr/bin/env python
# (c) 2024 Niels Provos
#

from PIL import Image
import torch
from transformers import AutoImageProcessor, Swin2SRForImageSuperResolution
import numpy as np

from utils import torch_get_device


def upscale_tile(model, image_processor, tile):
    """
    Upscales a tile using a given model and image processor.

    Args:
        model: The model used for upscaling the tile.
        image_processor: The image processor used to preprocess the tile.
        tile: The input tile to be upscaled.

    Returns:
        An Image object representing the upscaled tile.
    """
    inputs = image_processor(tile, return_tensors="pt")
    inputs = {name: tensor.to(model.device) for name, tensor in inputs.items()}

    with torch.no_grad():
        outputs = model(**inputs)

    output = outputs.reconstruction.squeeze().float().cpu().clamp_(0, 1).numpy()
    output = np.moveaxis(output, source=0, destination=-1)
    output = (output * 255.0).round().astype(np.uint8)

    return Image.fromarray(output)


def integrate_tile(tile, image, left, top, right, bottom, tile_x, tile_y, overlap):
    if tile_x > 0:
        left_strip = tile.crop((0, 0, overlap, tile.height))
        blended = Image.blend(left_strip, image.crop(
            (left, top, left + overlap, bottom)), 0.5)
        image.paste(blended, (left, top, left + overlap, bottom))
        if overlap > tile.width:
            # Skip the tile if the overlap is greater than the tile width
            return
        tile = tile.crop((overlap, 0, tile.width, tile.height))
        left += overlap
    if tile_y > 0:
        top_strip = tile.crop((0, 0, tile.width, overlap))
        blended = Image.blend(top_strip, image.crop(
            (left, top, right, top + overlap)), 0.5)
        image.paste(blended, (left, top, right, top + overlap))
        if overlap > tile.height:
            # Skip the tile if the overlap is greater than the tile height
            return
        tile = tile.crop((0, overlap, tile.width, tile.height))
        top += overlap
    # Paste the upscaled tile onto the upscaled image
    image.paste(tile, (left, top, right, bottom))


def upscale_image_tiled(image_path, tile_size=512, overlap=64):
    """
    Upscales an image using a tiled approach.

    Args:
        image_path (str): The path to the input image file.
        tile_size (int, optional): The size of each tile. Defaults to 512.
        overlap (int, optional): The overlap between adjacent tiles. Defaults to 64.

    Returns:
        PIL.Image.Image: The upscaled image.
    """
    # Load the image
    image = Image.open(image_path).convert("RGB")

    # Initialize the image processor and model
    image_processor = AutoImageProcessor.from_pretrained(
        "caidas/swin2SR-classical-sr-x2-64")
    model = Swin2SRForImageSuperResolution.from_pretrained(
        "caidas/swin2SR-classical-sr-x2-64")
    model.to(torch_get_device())

    # Calculate the number of tiles
    width, height = image.size
    step_size = tile_size - overlap
    num_tiles_x = (width + step_size - 1) // step_size
    num_tiles_y = (height + step_size - 1) // step_size

    # Create a new image to store the upscaled result
    upscaled_width = width * 2
    upscaled_height = height * 2
    upscaled_image = Image.new("RGB", (upscaled_width, upscaled_height))

    # Iterate over the tiles
    for y in range(num_tiles_y):
        for x in range(num_tiles_x):
            # Calculate the coordinates of the current tile
            left = x * step_size
            top = y * step_size
            right = min(left + tile_size, width)
            bottom = min(top + tile_size, height)

            print(
                f"Processing tile ({y}, {x} with coordinates ({left}, {top}, {right}, {bottom})")

            # Extract the current tile from the image
            tile = image.crop((left, top, right, bottom))
            upscaled_tile = upscale_tile(model, image_processor, tile)

            # Calculate the coordinates to paste the upscaled tile
            place_left = x * step_size * 2
            place_top = y * step_size * 2
            place_right = place_left + upscaled_tile.width
            place_bottom = place_top + upscaled_tile.height

            integrate_tile(upscaled_tile, upscaled_image, place_left,
                           place_top, place_right, place_bottom, x, y, overlap)

    # Save the upscaled image
    return upscaled_image


if __name__ == "__main__":
    upscaled_image = upscale_image_tiled(
        "appstate-feBWVeXR/image_slice_1_v2.png", tile_size=512, overlap=64)
    upscaled_image.save("upscaled_image.png")
