#!/usr/bin/env python
# (c) 2024 Niels Provos

import base64
import cv2
import datetime
import io
import os
from pathlib import Path
from PIL import Image
import numpy as np
from segmentation import (
    generate_depth_map,
    analyze_depth_histogram,
    generate_image_slices,
    setup_camera_and_cards,
    export_gltf,
    blend_with_alpha,
    render_image_sequence
)
import components
from utils import filename_add_version, find_pixel_from_click, postprocess_depth_map, get_gltf_iframe, get_no_gltf_available
from depth import DepthEstimationModel

import dash
from dash import dcc, html, ctx, no_update
from dash.dependencies import Input, Output, State, ClientsideFunction
from dash.dependencies import ALL, MATCH
from dash_extensions import EventListener
from dash.exceptions import PreventUpdate
from flask import send_file

from controller import AppState


# Progress tracking variables
current_progress = -1
total_progress = 100


def progress_callback(current, total):
    global current_progress, total_progress
    current_progress = (current / total) * 100
    total_progress = 100

# call the ability to add external scripts
external_scripts = [
    # add the tailwind cdn url hosting the files with the utility classes
    {'src': 'https://cdn.tailwindcss.com'},
    {'src': 'https://kit.fontawesome.com/48f728cfc9.js'},
]

app = dash.Dash(__name__,
                external_scripts=external_scripts)

# Create a Flask route for serving images
@app.server.route(f'/{AppState.SRV_DIR}/<path:filename>')
def serve_data(filename):
    filename = Path(os.getcwd()) / filename
    if filename.suffix == '.gltf':
        mimetype = 'model/gltf+json'
    else:
        mimetype = f'image/{filename.suffix[1:]}'
    print(f"Sending {filename} with mimetype {mimetype}")
    return send_file(str(filename), mimetype=mimetype)

# JavaScript event(s) that we want to listen to and what properties to collect.
eventScroll = {"event": "scroll", "props": ["type", "scrollLeft", "scrollTop"]}

app.layout = html.Div([
    EventListener(events=[eventScroll], logging=True, id="evScroll"),
    # dcc.Store stores all application state
    dcc.Store(id='application-state-filename'),
    dcc.Store(id='restore-state'),  # trigger to restore state
    dcc.Store(id='rect-data'),  # Store for rect coordinates
    dcc.Store(id='logs-data', data=[]),  # Store for logs
    # Trigger for generating depth map
    dcc.Store(id='trigger-generate-depthmap'),
    dcc.Store(id='trigger-update-depthmap'),  # Trigger for updating depth map
    # Trigger for updating thresholds
    dcc.Store(id='update-thresholds-container'),
    # App Layout
    html.Header("Parallax Maker",
                className='text-2xl font-bold bg-blue-800 text-white p-2 mb-4 text-center'),
    html.Main([
        components.make_tabs(
            'viewer',
            ['2D', '3D'],
            [
                components.make_input_image_container(
                    upload_id='upload-image',
                    image_id='image', event_id='el',
                    canvas_id='canvas',
                    outer_class_name='w-full col-span-3'),
                html.Div(
                    id="model-viewer-container",
                    children=[
                        html.Iframe(
                            id="model-viewer",
                            srcDoc=get_no_gltf_available(),
                            style={'height': '70vh'},
                            className='w-full h-full bg-gray-400 p-2'
                        )
                    ]
                ),
            ],
            outer_class_name='w-full col-span-3'
        ),
        components.make_tabs(
            'main',
            ['Segmentation', 'Slice Generation',
                'Inpainting', 'Export', 'Configuration'],
            [
                html.Div([
                    components.make_depth_map_container(
                        depth_map_id='depth-map-container'),
                    components.make_thresholds_container(
                        thresholds_id='thresholds-container'),
                ], className='w-full', id='depth-map-column'),
                components.make_slice_generation_container(),
                components.make_inpainting_container(),
                html.Div([
                    components.make_3d_export_div(),
                    components.make_animation_export_div(),
                ],
                    className='w-full'
                ),
                components.make_configuration_div()
            ],
            outer_class_name='w-full col-span-2'
        ),
    ], className='grid grid-cols-5 gap-4 p-2'),
    components.make_logs_container(logs_id='log'),
    html.Footer('© 2024 Niels Provos',
                className='text-center text-gray-500 p-2'),
])

app.scripts.config.serve_locally = True

app.clientside_callback(
    ClientsideFunction(namespace='clientside',
                       function_name='store_rect_coords'),
    Output('rect-data', 'data'),
    Input('image', 'src'),
    Input('evScroll', 'n_events'),
)


components.make_canvas_callbacks(app)
components.make_navigation_callbacks(app)
components.make_inpainting_container_callbacks(app)


# Callbacks for collapsible sections
components.make_tabs_callback(app, 'viewer')
components.make_tabs_callback(app, 'main')


@app.callback(
    Output('canvas', 'className'),
    Output('image', 'className'),
    Output('canvas-buttons', 'className'),
    Input({'type': 'tab-content-main', 'index': ALL}, 'className'),
    State('canvas', 'className'),
    State('image', 'className'),
    State('canvas-buttons', 'className'),
)
def update_events(tab_class_names, canvas_class_name, image_class_name, buttons_class_name):
    if tab_class_names is None:
        raise PreventUpdate()

    canvas_class_name = canvas_class_name.replace(
        ' z-10', '').replace(' z-0', '')
    image_class_name = image_class_name.replace(
        ' z-10', '').replace(' z-0', '')
    buttons_class_name = buttons_class_name.replace(' hidden', '')

    # we paint on the canvas if the Segmentation or Inpainting tab is active
    if 'hidden' in tab_class_names[1] and 'hidden' in tab_class_names[2]:
        canvas_class_name += ' z-0'
        image_class_name += ' z-10'
        buttons_class_name += ' hidden'
    else:
        canvas_class_name += ' z-10'
        image_class_name += ' z-0'

    return canvas_class_name, image_class_name, buttons_class_name


# Callback for the logs


@app.callback(Output('log', 'children'),
              Input('logs-data', 'data'),
              prevent_initial_call=True)
def update_logs(data):
    structured_logs = [html.Div(log) for log in data[-3:]]
    return structured_logs

# Callback to update progress bar


@app.callback(
    Output('progress-bar-container', 'children'),
    Output('progress-interval', 'disabled', allow_duplicate=True),
    Input('progress-interval', 'n_intervals'),
    prevent_initial_call=True
)
def update_progress(n):
    progress_bar = html.Div(className='w-0 h-full bg-green-500 rounded-lg transition-all',
                            style={'width': f'{max(0, current_progress)}%'})
    interval_disabled = current_progress >= total_progress or current_progress == -1
    return progress_bar, interval_disabled


@app.callback(
    Output({'type': 'threshold-slider', 'index': ALL}, 'value'),
    Output('slice-img-container', 'children', allow_duplicate=True),
    Output('image', 'src', allow_duplicate=True),
    Input({'type': 'threshold-slider', 'index': ALL}, 'value'),
    State('num-slices-slider', 'value'),
    State('application-state-filename', 'data'),
    prevent_initial_call=True
)
def update_threshold_values(threshold_values, num_slices, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    if state.imgThresholds[1:-1] == threshold_values:
        print("Threshold values are the same; not erasing data.")
        raise PreventUpdate()

    # make sure that threshold values are monotonically increasing
    if threshold_values[0] <= 0:
        threshold_values[0] = 1

    for i in range(1, num_slices-1):
        if threshold_values[i] <= threshold_values[i-1]:
            threshold_values[i] = threshold_values[i-1] + 1

    # go through the list in reverse order to make sure that the thresholds are monotonically decreasing
    if threshold_values[-1] >= 255:
        threshold_values[-1] = 254

    # num slices is the number of thresholds + 1, so the largest index is num_slices - 2
    # and the second largest index is num_slices - 3
    for i in range(num_slices-3, -1, -1):
        if threshold_values[i] >= threshold_values[i+1]:
            threshold_values[i] = threshold_values[i+1] - 1

    state.imgThresholds[1:-1] = threshold_values
    state.image_slices = []
    state.image_slices_filenames = []

    img_data = no_update
    if state.slice_pixel:
        img_data, _ = state.depth_slice_from_pixel(
            state.slice_pixel[0], state.slice_pixel[1])

    return threshold_values, None, img_data


@app.callback(
    Output('generate-slice-request', 'data', allow_duplicate=True),
    Input('num-slices-slider-update', 'data'),
    State('application-state-filename', 'data'),
    prevent_initial_call=True)
def update_num_slices(value, filename):
    """Updates the slices only if we have them already."""
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    if len(state.image_slices) == 0:
        raise PreventUpdate()

    return True


@app.callback(
    Output('thresholds-container', 'children'),
    Input('update-thresholds-container', 'data'),
    State('application-state-filename', 'data'),
    prevent_initial_call=True
)
def update_thresholds_html(value, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    thresholds = []
    for i in range(1, state.num_slices):
        threshold = html.Div([
            dcc.Slider(
                id={'type': 'threshold-slider', 'index': i},
                min=0,
                max=255,
                step=1,
                value=state.imgThresholds[i],
                marks=None,
                tooltip={'always_visible': True, 'placement': 'bottom'}
            )
        ], className='m-2')
        thresholds.append(threshold)

    return thresholds


@app.callback(
    Output('update-thresholds-container', 'data', allow_duplicate=True),
    Output('logs-data', 'data', allow_duplicate=True),
    # triggers regeneration of slices if we have them already
    Output('num-slices-slider-update', 'data'),
    Input('depth-map-container', 'children'),
    Input('num-slices-slider', 'value'),
    State('application-state-filename', 'data'),
    State('logs-data', 'data'),
    prevent_initial_call=True
)
def update_thresholds(contents, num_slices, filename, logs_data):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if (
            state.num_slices == num_slices and
            state.imgThresholds is not None and
            len(state.imgThresholds) == num_slices + 1):
        print("Number of slices is the same; not erasing data.")
        raise PreventUpdate()

    state.num_slices = num_slices

    if state.depthMapData is None:
        logs_data.append("No depth map data available")
        state.imgThresholds = [0]
        state.imgThresholds.extend([i * (255 // (num_slices - 1))
                                    for i in range(1, num_slices)])
    elif state.imgThresholds is None or len(state.imgThresholds) != num_slices:
        state.imgThresholds = analyze_depth_histogram(
            state.depthMapData, num_slices=num_slices)

    logs_data.append(f"Thresholds: {state.imgThresholds}")

    return True, logs_data, True


@app.callback(Output('application-state-filename', 'data', allow_duplicate=True),
              Output('trigger-generate-depthmap',
                     'data', allow_duplicate=True),
              Output('image', 'src', allow_duplicate=True),
              Output('depth-map-container', 'children', allow_duplicate=True),
              Output('progress-interval', 'disabled', allow_duplicate=True),
              Input('upload-image', 'contents'),
              prevent_initial_call=True)
def update_input_image(contents):
    if not contents:
        raise PreventUpdate()

    state, filename = AppState.from_file_or_new(None)

    content_type, content_string = contents.split(',')

    # save the image data to the state
    state.set_img_data(Image.open(io.BytesIO(base64.b64decode(content_string))))

    img_uri = state.serve_input_image()

    return filename, True, img_uri, html.Img(
        id='depthmap-image',
        className='w-full p-0 object-scale-down'), False


@app.callback(Output('image', 'src', allow_duplicate=True),
              Output('logs-data', 'data'),
              Input("el", "n_events"),
              State("el", "event"),
              State('rect-data', 'data'),
              State('application-state-filename', 'data'),
              State('logs-data', 'data'),
              prevent_initial_call=True
              )
def click_event(n_events, e, rect_data, filename, logs_data):
    if filename is None:
        raise PreventUpdate()
    
    state = AppState.from_cache(filename)

    if e is None or rect_data is None or state.imgData is None:
        raise PreventUpdate()

    clientX = e["clientX"]
    clientY = e["clientY"]

    rectTop = rect_data["top"]
    rectLeft = rect_data["left"]
    rectWidth = rect_data["width"]
    rectHeight = rect_data["height"]

    x = clientX - rectLeft
    y = clientY - rectTop

    pixel_x, pixel_y = find_pixel_from_click(state.imgData, x, y, rectWidth, rectHeight)
    
    img_data, depth = state.depth_slice_from_pixel(pixel_x, pixel_y)
    state.slice_pixel = (pixel_x, pixel_y)

    logs_data.append(
        f"Click event at ({clientX}, {clientY}) R:({rectLeft}, {rectTop}) in pixel coordinates ({pixel_x}, {pixel_y}) at depth {depth}")

    return img_data, logs_data


@app.callback(Output('trigger-update-depthmap', 'data'),
              Output('gen-depthmap-output', 'children'),
              Input('trigger-generate-depthmap', 'data'),
              State('application-state-filename', 'data'),
              State('depth-module-dropdown', 'value'),
              prevent_initial_call=True)
def generate_depth_map_callback(ignored_data, filename, model):
    if filename is None:
        raise PreventUpdate()

    print('Received a request to generate a depth map for state f{filename}')
    state = AppState.from_cache(filename)

    PIL_image = state.imgData

    if PIL_image.mode == 'RGBA':
        PIL_image = PIL_image.convert('RGB')

    np_image = np.array(PIL_image)
    
    depth_model = DepthEstimationModel(model=model)
    if depth_model != state.depth_estimation_model:
        state.depth_estimation_model = depth_model
    
    state.depthMapData = generate_depth_map(
        np_image, model=state.depth_estimation_model, progress_callback=progress_callback)
    state.imgThresholds = None

    return True, ""


@app.callback(Output('depth-map-container', 'children'),
              Input('trigger-update-depthmap', 'data'),
              State('application-state-filename', 'data'),
              prevent_initial_call=True)
def update_depth_map_callback(ignored_data, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    depth_map_pil = Image.fromarray(state.depthMapData)

    buffered = io.BytesIO()
    depth_map_pil.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')

    return html.Img(
        src='data:image/png;base64,{}'.format(img_str),
        className='w-full h-full object-contain',
        style={'height': '35vh'},
        id='depthmap-image'), ""


@app.callback(Output('generate-slice-request', 'data'),
              Input('generate-slice-button', 'n_clicks'))
def generate_slices_request(n_clicks):
    if n_clicks is None:
        raise PreventUpdate()
    return n_clicks


@app.callback(Output('slice-img-container', 'children'),
              Output('gen-slice-output', 'children', allow_duplicate=True),
              Output('image', 'src', allow_duplicate=True),
              Input('update-slice-request', 'data'),
              State('application-state-filename', 'data'),
              prevent_initial_call=True)
def update_slices(ignored_data, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.depthMapData is None:
        raise PreventUpdate()

    caret_color_enabled = "text-emerald-400"
    caret_color_disabled = "text-orange-800"

    img_container = []
    assert len(state.image_slices) == len(state.image_slices_filenames)
    for i, img_slice in enumerate(state.image_slices):
        img_data = state.serve_slice_image(i)
        
        left_color = caret_color_enabled if state.can_undo(i, forward=False) else caret_color_disabled
        left_disabled = True if left_color == caret_color_disabled else False
        right_color = caret_color_enabled if state.can_undo(i, forward=True) else caret_color_disabled
        right_disabled = True if right_color == caret_color_disabled else False
        
        left_id = {'type': 'slice-undo-backwards', 'index': i}
        right_id = {'type': 'slice-undo-forwards', 'index': i}
        
        slice_name = html.Div([
            html.Button(
                title="Download image for manipuation in an external editor",
                className="fa-solid fa-download pr-1",
                id={'type': 'slice-info', 'index': i}),
            html.Button(
                title="Undo last change",
                className=f"fa-solid fa-caret-left {left_color} pr-1",
                id=left_id, disabled=left_disabled),
            html.Button(
                title="Redo last change",
                className=f"fa-solid fa-caret-right {right_color} pr-1",
                id=right_id, disabled=right_disabled),
            Path(state.image_slices_filenames[i]).stem])
        img_container.append(
            dcc.Upload(
                html.Div([
                    html.Img(
                        src=img_data,
                        className='w-full h-full object-contain border-solid border-2 border-slate-500',
                        id={'type': 'slice', 'index': i},),
                    html.Div(children=slice_name,
                             className='text-center text-overlay p-1')
                ], style={'position': 'relative'}),
                id={'type': 'slice-upload', 'index': i},
                disable_click=True,
            )
        )
        
    img_data = no_update
    if state.selected_slice is not None:
        assert state.selected_slice >= 0 and state.selected_slice < len(state.image_slices)
        img_data = state.serve_slice_image(state.selected_slice)
        state.slice_pixel = None

    return img_container, "", img_data

@app.callback(Output('update-slice-request', 'data', allow_duplicate=True),
             Input({'type': 'slice-undo-backwards', 'index': ALL}, 'n_clicks'),
             Input({'type': 'slice-undo-forwards', 'index': ALL}, 'n_clicks'),
             State('application-state-filename', 'data'),
             prevent_initial_call=True)
def undo_slice(n_clicks_backwards, n_clicks_forwards, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    # don't need to use ctx.triggered_id since we are repaining the whole thing
    index = None
    forward = None
    if any(n_clicks_backwards):
        index = n_clicks_backwards.index(1)
        forward = False
    elif any(n_clicks_forwards):
        index = n_clicks_forwards.index(1)
        forward = True
    else:
        raise PreventUpdate()

    if not state.undo(index, forward=forward):
        print(f"Cannot undo slice {index} with forward {forward}")
        raise PreventUpdate()

    # only save the json with the updated file mapping
    state.to_file(filename, save_image_slices=False, save_depth_map=False, save_input_image=False)

    return True



@app.callback(Output('update-slice-request', 'data', allow_duplicate=True),
              Output('gen-slice-output', 'children'),
              Input('generate-slice-request', 'data'),
              State('application-state-filename', 'data'),
              prevent_initial_call=True)
def generate_slices(ignored_data, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.depthMapData is None:
        raise PreventUpdate()

    state.image_slices = generate_image_slices(
        np.array(state.imgData),
        state.depthMapData,
        state.imgThresholds,
        num_expand=5)
    state.image_slices_filenames = []

    print(f'Generated {len(state.image_slices)} image slices; saving to file')
    state.to_file(filename)

    return True, ""


@app.callback(Output('image', 'src'),
              Output({'type': 'slice', 'index': ALL}, 'n_clicks'),
              Input({'type': 'slice', 'index': ALL}, 'n_clicks'),
              State({'type': 'slice', 'index': ALL}, 'id'),
              State({'type': 'slice', 'index': ALL}, 'src'),
              State('application-state-filename', 'data'),
              prevent_initial_call=True)
def display_slice(n_clicks, id, src, filename):
    if filename is None or n_clicks is None or any(n_clicks) is False:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    index = n_clicks.index(1)

    state.selected_slice = index

    return src[index], [None]*len(n_clicks)


@app.callback(Output('logs-data', 'data', allow_duplicate=True),
              Output('gltf-loading', 'children', allow_duplicate=True),
              Input('upscale-textures', 'n_clicks'),
              State('application-state-filename', 'data'),
              State('logs-data', 'data'),
              prevent_initial_call=True)
def upscale_texture(n_clicks, filename, logs):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    state.upscale_slices()

    logs.append("Upscaled textures for slices")

    return logs, ""


@app.callback(Output('download-gltf', 'data'),
              Output('gltf-loading', 'children', allow_duplicate=True),
              Input('gltf-export', 'n_clicks'),
              State('application-state-filename', 'data'),
              State('camera-distance-slider', 'value'),
              State('max-distance-slider', 'value'),
              State('focal-length-slider', 'value'),
              State('displacement-slider', 'value'),
              prevent_initial_call=True
              )
def gltf_export(n_clicks, filename, camera_distance, max_distance, focal_length, displacement_scale):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    gltf_path = export_state_as_gltf(state, filename, camera_distance, max_distance, focal_length, displacement_scale)

    return dcc.send_file(gltf_path, filename='scene.gltf'), ""


# XXX - this and the callback above can be chained to avoid code duplication
@app.callback(Output('model-viewer', 'srcDoc', allow_duplicate=True),
              Output('gltf-loading', 'children', allow_duplicate=True),
              Input('gltf-create', 'n_clicks'),
              State('application-state-filename', 'data'),
              State('camera-distance-slider', 'value'),
              State('max-distance-slider', 'value'),
              State('focal-length-slider', 'value'),
              State('displacement-slider', 'value'),
              prevent_initial_call=True
              )
def gltf_create(n_clicks, filename, camera_distance, max_distance, focal_length, displacement_scale):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    export_state_as_gltf(
        state, filename, camera_distance, max_distance, focal_length, displacement_scale)

    return get_gltf_iframe(state.serve_model_file()), ""




def export_state_as_gltf(state, filename, camera_distance, max_distance, focal_length, displacement_scale):
    camera_matrix, card_corners_3d_list = setup_camera_and_cards(
        state.image_slices,
        state.imgThresholds, camera_distance, max_distance, focal_length)

    depth_filenames = []
    if displacement_scale > 0:
        for i, image in enumerate(state.image_slices):
            print(f"Generating depth map for slice {i}")
            depth_filename = state.depth_filename(i)
            if not depth_filename.exists():
                model = DepthEstimationModel(model='midas')
                if model != state.depth_estimation_model:
                    state.depth_estimation_model = model
                depth_map = generate_depth_map(image[:, :, :3], model=state.depth_estimation_model)
                depth_map = postprocess_depth_map(depth_map, image[:, :, 3])
                Image.fromarray(depth_map).save(depth_filename, compress_level=1)
            depth_filenames.append(depth_filename)

    # check whether we have upscaled slices we should use
    slices_filenames = []
    for i, slice_filename in enumerate(state.image_slices_filenames):
        upscaled_filename = state.upscaled_filename(i)
        if upscaled_filename.exists():
            slices_filenames.append(upscaled_filename)
        else:
            slices_filenames.append(slice_filename)

    aspect_ratio = float(camera_matrix[0, 2]) / camera_matrix[1, 2]
    output_path = Path(filename) / state.MODEL_FILE
    gltf_path = export_gltf(output_path, aspect_ratio, focal_length, camera_distance,
                            card_corners_3d_list, slices_filenames, depth_filenames,
                            displacement_scale=displacement_scale)
                            
    return gltf_path


@app.callback(Output('download-image', 'data'),
              Input({'type': 'slice-info', 'index': ALL}, 'n_clicks'),
              State('application-state-filename', 'data'),
              prevent_initial_call=True)
def download_image(n_clicks, filename):
    if filename is None or n_clicks is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    index = ctx.triggered_id['index']
    if n_clicks[index] is None:
        raise PreventUpdate()

    # print(n_clicks, index, ctx.triggered)

    image_path = state.image_slices_filenames[index]

    return dcc.send_file(image_path, Path(state.image_slices_filenames[index]).name)


@app.callback(Output('update-slice-request', 'data', allow_duplicate=True),
              Output('logs-data', 'data', allow_duplicate=True),
              Input({'type': 'slice-upload', 'index': ALL}, 'contents'),
              State('application-state-filename', 'data'),
              State('logs-data', 'data'),
              prevent_initial_call=True)
def slice_upload(contents, filename, logs):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if len(state.image_slices) == 0:
        raise PreventUpdate()

    index = ctx.triggered_id['index']
    if contents[index] is None:
        raise PreventUpdate()

    content = contents[index]
    image = Image.open(io.BytesIO(base64.b64decode(content.split(',')[1])))
    state.image_slices[index] = np.array(image)

    # add a version number to the filename and increase if it already exists
    image_filename = filename_add_version(state.image_slices_filenames[index])
    state.image_slices_filenames[index] = image_filename

    composed_image = state.image_slices[0].copy()
    for i, slice_image in enumerate(state.image_slices[1:]):
        blend_with_alpha(composed_image, slice_image)
    state.imgData = Image.fromarray(composed_image)

    logs.append(
        f"Received image slice upload for slice {index} at {image_filename}")

    state.to_file(filename)

    return True, logs


@app.callback(Output('logs-data', 'data', allow_duplicate=True),
              Output('gen-animation-output', 'children'),
              Input('animation-export', 'n_clicks'),
              State('application-state-filename', 'data'),
              State('number-of-frames-slider', 'value'),
              State('logs-data', 'data'),
              prevent_initial_call=True)
def export_animation(n_clicks, filename, num_frames, logs):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    camera_distance = 100.0
    max_distance = 500.0
    focal_length = 100.0
    camera_matrix, card_corners_3d_list = setup_camera_and_cards(
        state.image_slices, state.imgThresholds, camera_distance, max_distance, focal_length)

    # Render the initial view
    camera_position = np.array([0, 0, -100], dtype=np.float32)
    render_image_sequence(
        filename,
        state.image_slices, card_corners_3d_list, camera_matrix, camera_position,
        num_frames=num_frames)

    logs.append(f"Exported {num_frames} frames to animation")

    return logs, ""

@app.callback(
    Output('model-viewer', 'srcDoc'),
    Input('restore-state', 'data'),
    State('application-state-filename', 'data'),
    prevent_initial_call=True)
def update_model_viewer(value, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    iframe = get_gltf_iframe(state.serve_model_file())

    return iframe


@app.callback(Output('update-slice-request', 'data', allow_duplicate=True),
              Input('restore-state', 'data'),
              State('application-state-filename', 'data'),
              prevent_initial_call=True)
def restore_state_slices(value, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if len(state.image_slices) == 0:
        print("No image slices to restore")
        raise PreventUpdate()

    return True


@app.callback(Output('trigger-update-depthmap', 'data', allow_duplicate=True),
              Input('restore-state', 'data'),
              prevent_initial_call=True)
def restore_state_depthmap(value):
    return True


@app.callback(
    # XXX - generate depth-map via separate callback
    Output('application-state-filename', 'data'),
    Output('restore-state', 'data'),
    Output('image', 'src', allow_duplicate=True),
    Output('update-thresholds-container', 'data', allow_duplicate=True),
    Output('num-slices-slider', 'value'),
    Output('logs-data', 'data', allow_duplicate=True),
    Input('upload-state', 'contents'),
    State('logs-data', 'data'),
    prevent_initial_call=True)
def restore_state(contents, logs):
    if contents is None:
        raise PreventUpdate()

    # decode the contents into json
    content_type, content_string = contents.split(',')
    decoded_contents = base64.b64decode(content_string).decode('utf-8')
    state = AppState.from_json(decoded_contents)
    state.fill_from_files(state.filename)
    AppState.cache[state.filename] = state  # XXX - this may be too hacky

    logs.append(f"Restored state from {state.filename}")

    # XXX - refactor this to be triggered by a write to restore-state
    buffered = io.BytesIO()
    state.imgData.save(buffered, format="PNG")
    img_data = base64.b64encode(buffered.getvalue()).decode('utf-8')
    img_data = f"data:image/png;base64,{img_data}"

    return state.filename, True, img_data, True, state.num_slices, logs


@app.callback(Output('logs-data', 'data', allow_duplicate=True),
              Input('save-state', 'n_clicks'),
              State('application-state-filename', 'data'),
              State('logs-data', 'data'),
              prevent_initial_call=True)
def save_state(n_clicks, filename, logs):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    state.to_file(filename)

    logs.append(f"Saved state to {filename}")

    return logs


if __name__ == '__main__':
    os.environ['DISABLE_TELEMETRY'] = 'YES'
    os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
    app.run_server(debug=True)
