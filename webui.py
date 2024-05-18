#!/usr/bin/env python
# (c) 2024 Niels Provos

import argparse
import base64
import io
import os
from pathlib import Path
from PIL import Image
import numpy as np

import constants as C
from segmentation import (
    generate_depth_map,
    analyze_depth_histogram,
    generate_image_slices,
    create_slice_from_mask,
    setup_camera_and_cards,
    export_gltf,
    blend_with_alpha,
    remove_mask_from_alpha,
    render_image_sequence
)
import components
from utils import (
    filename_add_version, find_pixel_from_click, postprocess_depth_map, get_gltf_iframe, get_no_gltf_available,
    highlight_selected_element
)
from depth import DepthEstimationModel
from instance import SegmentationModel
from inpainting import InpaintingModel


import dash
from dash import dcc, html, ctx, no_update
from dash.dependencies import Input, Output, State, ClientsideFunction
from dash.dependencies import ALL, MATCH
from dash_extensions import EventListener
from dash.exceptions import PreventUpdate
from flask import send_file
from werkzeug import serving

from controller import AppState

# Globals
EXPAND_MASK = 5
HIGHLIGHT_COLOR = 'bg-green-200'

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
    dcc.Store(id=C.STORE_APPSTATE_FILENAME),
    dcc.Store(id=C.STORE_RESTORE_STATE),  # trigger to restore state
    dcc.Store(id=C.STORE_RECT_DATA),  # Store for rect coordinates
    dcc.Store(id=C.LOGS_DATA, data=[]),  # Store for logs
    # Trigger for generating depth map
    dcc.Store(id=C.STORE_TRIGGER_GEN_DEPTHMAP),
    dcc.Store(id=C.STORE_TRIGGER_UPDATE_DEPTHMAP),  # Trigger for updating depth map
    # Trigger for updating thresholds
    dcc.Store(id=C.STORE_UPDATE_THRESHOLD_CONTAINER),
    # Context for the help window
    dcc.Store(id=C.STORE_CURRENT_TAB),
    # App Layout
    html.Header("Parallax Maker",
                className='text-2xl font-bold bg-blue-800 text-white p-2 mb-4 text-center'),
    html.Main([
        html.Div(
            ['Some helpful text to guide the user'],
            id=C.CTR_HELP_WINDOW,
            className='hidden absolute w-64 p-2 bg-yellow-100 border border-black z-40'),
        components.make_tabs(
            'viewer',
            ['2D', '3D'],
            [
                components.make_input_image_container(
                    upload_id=C.UPLOAD_IMAGE,
                    image_id=C.IMAGE, event_id='el',
                    canvas_id=C.CANVAS,
                    outer_class_name='w-full col-span-3'),
                html.Div(
                    id=C.CTR_MODEL_VIEWER,
                    children=[
                        html.Iframe(
                            id=C.IFRAME_MODEL_VIEWER,
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
            ['Mode', 'Segmentation', 'Inpainting', 'Export', 'Configuration'],
            [
                html.Div([
                    components.make_depth_map_container(
                        depth_map_id=C.CTR_DEPTH_MAP),
                    components.make_mode_selector(),
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
    Output(C.STORE_RECT_DATA, 'data'),
    Input(C.IMAGE, 'src'),
    Input('evScroll', 'n_events'),
)

app.clientside_callback(
    ClientsideFunction(namespace='clientside',
                       function_name='suppress_contextmenu'),
    Output(C.CTR_INPUT_IMAGE, 'id'), Input(C.CTR_INPUT_IMAGE, 'id')
)

app.clientside_callback(
    ClientsideFunction(namespace='clientside',
                       function_name='store_current_tab'),
    Output(C.STORE_CURRENT_TAB, 'data'), Input(C.STORE_CURRENT_TAB, 'data')
)

components.make_segmentation_callbacks(app)
components.make_canvas_callbacks(app)
components.make_navigation_callbacks(app)
components.make_inpainting_container_callbacks(app)
components.make_configuration_callbacks(app)


# Callbacks for collapsible sections
components.make_tabs_callback(app, 'viewer')
components.make_tabs_callback(app, 'main')
components.make_tools_callbacks(app)


# Callback for the logs


@app.callback(Output('log', 'children'),
              Input(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def update_logs(data):
    structured_logs = [html.Div(log) for log in data[-3:]]
    return structured_logs

# Callback to update progress bar


@app.callback(
    Output(C.CTR_PROGRESS_BAR, 'children'),
    Output(C.PROGRESS_INTERVAL, 'disabled', allow_duplicate=True),
    Input(C.PROGRESS_INTERVAL, 'n_intervals'),
    prevent_initial_call=True
)
def update_progress(n):
    progress_bar = html.Div(className='w-0 h-full bg-green-500 rounded-lg transition-all',
                            style={'width': f'{max(0, current_progress)}%'})
    interval_disabled = current_progress >= total_progress or current_progress == -1
    return progress_bar, interval_disabled


@app.callback(
    Output({'type': 'threshold-slider', 'index': ALL}, 'value'),
    Output(C.IMAGE, 'src', allow_duplicate=True),
    Input({'type': 'threshold-slider', 'index': ALL}, 'value'),
    State(C.SLIDER_NUM_SLICES, 'value'),
    State(C.STORE_APPSTATE_FILENAME, 'data'),
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

    img_data = no_update
    if state.slice_pixel:
        state.slice_mask, _ = state.depth_slice_from_pixel(
            state.slice_pixel[0], state.slice_pixel[1])
        if state.slice_mask is not None:
            result = state.apply_mask(state.imgData, state.slice_mask)
            img_data = state.serve_main_image(result)
        else:
            img_data = state.serve_main_image(state.imgData)

    return threshold_values, img_data


@app.callback(
    Output(C.CTR_THRESHOLDS, 'children'),
    Input(C.STORE_UPDATE_THRESHOLD_CONTAINER, 'data'),
    State(C.STORE_APPSTATE_FILENAME, 'data'),
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
                tooltip={'always_visible': True, 'placement': 'right'},
            )
        ], className='m-1 pl-1')
        thresholds.append(threshold)

    return thresholds


@app.callback(
    Output(C.STORE_UPDATE_THRESHOLD_CONTAINER, 'data', allow_duplicate=True),
    Output(C.LOGS_DATA, 'data', allow_duplicate=True),
    # triggers regeneration of slices if we have them already
    Input(C.CTR_DEPTH_MAP, 'children'),
    Input(C.SLIDER_NUM_SLICES, 'value'),
    State(C.STORE_APPSTATE_FILENAME, 'data'),
    State(C.LOGS_DATA, 'data'),
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

    return True, logs_data


@app.callback(Output(C.STORE_APPSTATE_FILENAME, 'data', allow_duplicate=True),
              Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.STORE_TRIGGER_GEN_DEPTHMAP,
                     'data', allow_duplicate=True),
              Output(C.IMAGE, 'src', allow_duplicate=True),
              Output(C.CTR_DEPTH_MAP, 'children', allow_duplicate=True),
              Output(C.PROGRESS_INTERVAL, 'disabled', allow_duplicate=True),
              Input(C.UPLOAD_IMAGE, 'contents'),
              State({'type': f'tab-content-main', 'index': ALL}, 'className'),
              prevent_initial_call=True)
def update_input_image(contents, classnames):
    if not contents:
        raise PreventUpdate()

    # allow an upload action only when the user is on the main tab
    # or configuration tab
    on_valid_tab = 'hidden' not in classnames[0] or 'hidden' not in classnames[-1]
    if classnames is None or not on_valid_tab:
        raise PreventUpdate()

    state, filename = AppState.from_file_or_new(None)

    content_type, content_string = contents.split(',')

    # save the image data to the state
    state.set_img_data(Image.open(
        io.BytesIO(base64.b64decode(content_string))))

    img_uri = state.serve_input_image()

    return filename, True, True, img_uri, html.Img(
        id='depthmap-image',
        className='w-full p-0 object-scale-down'), False


@app.callback(Output(C.IMAGE, 'src', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data'),
              Output(C.LOADING_UPLOAD, 'children', allow_duplicate=True),
              Input("el", "n_events"),
              State("el", "event"),
              State(C.STORE_RECT_DATA, 'data'),
              State(C.DROPDOWN_MODE_SELECTOR, 'value'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True
              )
def click_event(n_events, e, rect_data, mode, filename, logs_data):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    if e is None or rect_data is None or state.imgData is None:
        raise PreventUpdate()

    clientX = e["clientX"]
    clientY = e["clientY"]
    shiftClick = e["shiftKey"]
    ctrlClick = e["ctrlKey"]

    rectTop = rect_data["top"]
    rectLeft = rect_data["left"]
    rectWidth = rect_data["width"]
    rectHeight = rect_data["height"]

    x = clientX - rectLeft
    y = clientY - rectTop

    pixel_x, pixel_y = find_pixel_from_click(
        state.imgData, x, y, rectWidth, rectHeight)

    # we need to find the depth even if we use instance segmentation
    new_mask, depth = state.depth_slice_from_pixel(pixel_x, pixel_y)
    state.slice_pixel = (pixel_x, pixel_y)
    state.slice_pixel_depth = depth

    image = state.imgData
    if mode == 'segment':
        if state.segmentation_model == None:
            state.segmentation_model = SegmentationModel()
        # if we have a slice, take it and compose the background image over it
        print(f"Selected slice: {state.selected_slice}")
        if state.selected_slice is not None:
            image = state.slice_image_composed(state.selected_slice, grayscale=False)
        state.segmentation_model.segment_image(image)
        new_mask = state.segmentation_model.mask_at_point(
            (pixel_x, pixel_y))
        
    # allow mask manipulation with add and subtract via shift and ctrl click
    if state.slice_mask is None or not (shiftClick or ctrlClick):
        state.slice_mask = new_mask
    elif shiftClick:
        state.slice_mask = np.maximum(state.slice_mask, new_mask)
    elif ctrlClick:
        state.slice_mask = np.minimum(state.slice_mask, 255 - new_mask)

    if state.slice_mask is not None:
        result = state.apply_mask(image, state.slice_mask)
        img_data = state.serve_main_image(result)
    else:
        img_data = state.serve_main_image(state.imgData)

    logs_data.append(
        f"Click event at ({clientX}, {clientY}) R:({rectLeft}, {rectTop}) in pixel coordinates ({pixel_x}, {pixel_y}) at depth {depth}")

    return img_data, logs_data, ""

@app.callback(Output(C.STORE_TRIGGER_GEN_DEPTHMAP, 'data'),
              Input(C.BTN_GENERATE_DEPTHMAP, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              running=[(Output(C.BTN_GENERATE_DEPTHMAP, 'disabled'), True, False)],
              prevent_initial_call=True)
def generate_depth_map_from_button(n_clicks, filename):
    if n_clicks is None or filename is None:
        raise PreventUpdate()
    return True

@app.callback(Output(C.STORE_TRIGGER_UPDATE_DEPTHMAP, 'data'),
              Output(C.DEPTHMAP_OUTPUT, 'children'),
              Input(C.STORE_TRIGGER_GEN_DEPTHMAP, 'data'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.DROPDOWN_DEPTH_MODEL, 'value'),
              prevent_initial_call=True)
def generate_depth_map_callback(ignored_data, filename, model):
    if filename is None:
        raise PreventUpdate()

    print(f'Received a request to generate a depth map for state f{filename}')
    state = AppState.from_cache(filename)
    
    # we should update the depth model name more consistently
    state.depth_model_name = model

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


@app.callback(Output(C.CTR_DEPTH_MAP, 'children'),
              Input(C.STORE_TRIGGER_UPDATE_DEPTHMAP, 'data'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
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


@app.callback(Output(C.IMAGE, 'src', allow_duplicate=True),
              Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.LOADING_UPLOAD, 'children', allow_duplicate=True),
              Input(C.BTN_DELETE_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def delete_slice_request(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.selected_slice is None:
        logs.append("No slice selected")
        return no_update, no_update, logs, ""

    logs.append(f"Deleted slice at index {state.selected_slice}")

    state.delete_slice(state.selected_slice)

    # sufficient to just change the json.
    state.to_file(filename, save_image_slices=False,
                  save_depth_map=False, save_input_image=False)

    return state.serve_main_image(state.imgData), True, logs, ""

@app.callback(Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Input(C.BTN_COPY_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def copy_to_clipboard(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.slice_mask is None:
        logs.append("No mask selected")
        return logs
    
    if state.selected_slice is not None:
        image = state.slice_image_composed(state.selected_slice, grayscale=False).copy()
    else:
        image = np.array(state.imgData.convert('RGBA'))
    image[:,:,3] = state.slice_mask
    
    state.clipboard_image = image

    logs.append("Copied mask to clipboard")
    return logs


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.LOADING_UPLOAD, 'children', allow_duplicate=True),
              Input(C.BTN_PASTE_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def paste_clipboard_request(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.clipboard_image is None:
        logs.append("Nothing in the clipboard")
        return no_update, logs, no_update

    if state.selected_slice is None:
        logs.append("No slice selected")
        return no_update, logs, no_update

    image = state.clipboard_image
    # updates the image slice in place - dangerous
    blend_with_alpha(state.image_slices[state.selected_slice], image)

    image_filename = filename_add_version(
        state.image_slices_filenames[state.selected_slice])
    state.image_slices_filenames[state.selected_slice] = image_filename
    state.to_file(filename, save_image_slices=True,
                  save_depth_map=False, save_input_image=False)

    logs.append(f"Pasted clipboard to slice {state.selected_slice}")
    return True, logs, ""

@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.LOADING_UPLOAD, 'children', allow_duplicate=True),
              Input(C.BTN_REMOVE_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def remove_mask_slice_request(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.slice_mask is None:
        logs.append("No mask selected")
        return no_update, logs, no_update

    if state.selected_slice is None:
        logs.append("No slice selected")
        return no_update, logs, no_update

    final_mask = remove_mask_from_alpha(
        state.image_slices[state.selected_slice], state.slice_mask)
    state.image_slices[state.selected_slice][:, :, 3] = final_mask    

    image_filename = filename_add_version(
        state.image_slices_filenames[state.selected_slice])
    state.image_slices_filenames[state.selected_slice] = image_filename
    state.to_file(filename, save_image_slices=True,
                  save_depth_map=False, save_input_image=False)

    logs.append(f"Removed mask from slice {state.selected_slice}")
    return True, logs, ""

@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.LOADING_UPLOAD, 'children', allow_duplicate=True),
              Input(C.BTN_ADD_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def add_mask_slice_request(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.slice_mask is None:
        logs.append("No mask selected")
        return no_update, logs, no_update

    if state.selected_slice is None:
        logs.append("No slice selected")
        return no_update, logs, no_update

    image = create_slice_from_mask(
        state.imgData, state.slice_mask, num_expand=EXPAND_MASK)
    # updates the image slice in place - dangerous
    blend_with_alpha(state.image_slices[state.selected_slice], image)

    image_filename = filename_add_version(
        state.image_slices_filenames[state.selected_slice])
    state.image_slices_filenames[state.selected_slice] = image_filename
    state.to_file(filename, save_image_slices=True, save_depth_map=False, save_input_image=False)
    
    logs.append(f"Added mask to slice {state.selected_slice}")
    return True, logs, ""


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.TEXT_POSITIVE_PROMPT, 'value', allow_duplicate=True),
              Output(C.TEXT_NEGATIVE_PROMPT, 'value', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.LOADING_UPLOAD, 'children', allow_duplicate=True),
              Input(C.BTN_CREATE_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def create_single_slice_request(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.imgData is None:
        logs.append("No image available")
        return no_update, no_update, no_update, logs, no_update
    
    if state.slice_mask is None:
        # create an empty image that the user can inpaint if they want to
        image = Image.new('RGBA', state.imgData.size, (0, 0, 0, 0))
        depth = 127
    else:
        depth = state.slice_pixel_depth
        image = create_slice_from_mask(
            state.imgData, state.slice_mask, num_expand=EXPAND_MASK)
    state.selected_slice = state.add_slice(image, depth)
    # may need to optimize what is being saved eventually
    state.to_file(filename)

    logs.append("Created a slice from the mask")

    return True, "", "", logs, ""


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Input(C.BTN_BALANCE_SLICE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def balance_slices_request(n_clicks, filename, logs):
    if n_clicks is None:
        raise PreventUpdate()

    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if len(state.image_depths) == 0:
        raise PreventUpdate()

    state.balance_slices_depths()
    state.to_file(filename, save_image_slices=False,
                  save_depth_map=False, save_input_image=False)

    logs.append("Balanced slice depths")

    return True, logs


@app.callback(Output(C.STORE_GENERATE_SLICE, 'data'),
              Input(C.BTN_GENERATE_SLICE, 'n_clicks'))
def generate_slices_request(n_clicks):
    if n_clicks is None:
        raise PreventUpdate()
    return n_clicks


@app.callback(Output(C.CTR_SLICE_IMAGES, 'children'),
              Output('gen-slice-output', 'children', allow_duplicate=True),
              Output(C.IMAGE, 'src', allow_duplicate=True),
              Input(C.STORE_UPDATE_SLICE, 'data'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              prevent_initial_call=True)
def update_slices(ignored_data, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if len(state.image_slices) == 0:
        # a user may have uploaded a new image and not generated slices yet
        return [], "", no_update

    if state.depthMapData is None:
        raise PreventUpdate()

    caret_color_enabled = "text-emerald-400"
    caret_color_disabled = "text-orange-800"

    img_container = []
    assert len(state.image_slices) == len(state.image_slices_filenames)
    for i, img_slice in enumerate(state.image_slices):
        img_data = state.serve_slice_image(i)

        left_color = caret_color_enabled if state.can_undo(
            i, forward=False) else caret_color_disabled
        left_disabled = True if left_color == caret_color_disabled else False
        right_color = caret_color_enabled if state.can_undo(
            i, forward=True) else caret_color_disabled
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

        # slice creation with select a slice so we need to highlight it here
        highlight_class = f' {HIGHLIGHT_COLOR}' if state.selected_slice == i else ''
        img_container.append(
            dcc.Upload(
                html.Div([
                    html.Div(
                        # The number to display
                        children=f"{int(state.image_depths[i])}",
                        id={'type': 'depth-display', 'index': i},
                        className='absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-6xl text-amber-800 text-opacity-50 text-center'
                    ),
                    dcc.Input(id={'type': 'depth-input', 'index': i},
                              className='absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-center z-10 w-full h-full text-3xl sm:text-4xl md:text-5xl lg:text-6xl text-amber-800 text-opacity-50 hidden',
                              type='number',
                              debounce=True,
                              inputMode='numeric',
                              maxLength=3,
                              value=f"{int(state.image_depths[i])}"),
                    html.Img(
                        src=img_data,
                        className=f'w-full h-full object-contain border-solid border-2 border-slate-500{highlight_class}',
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
        assert state.selected_slice >= 0 and state.selected_slice < len(
            state.image_slices)
        img_data = state.serve_slice_image_composed(state.selected_slice)
        state.slice_pixel = None
        state.slice_mask = None
        state.slice_depth = None

    return img_container, "", img_data


@app.callback(
    Output({'type': 'depth-input', 'index': MATCH}, 'className'),
    Input({'type': 'depth-display', 'index': MATCH}, 'n_clicks'),
    State({'type': 'depth-input',  'index': MATCH}, 'className'),
    prevent_initial_call=True
)
def display_depth_input(n_clicks, class_name):
    if n_clicks is None:
        raise PreventUpdate()
    
    class_name = class_name.replace('hidden', '')
    return class_name


@app.callback(
    Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
    Input({'type': 'depth-input', 'index': ALL}, 'value'),
    Input({'type': 'depth-input', 'index': ALL}, 'n_submit'),
    State(C.STORE_APPSTATE_FILENAME, 'data'),
    prevent_initial_call=True
)
def record_depth_input(values, n_submits, filename):
    if filename is None or values is None or n_submits is None:
        raise PreventUpdate()
    
    index = ctx.triggered_id['index']
    if n_submits[index] is None:
        raise PreventUpdate()
    
    value = int(values[index])
    
    # need to re-order and validate the depth values
    state = AppState.from_cache(filename)
    
    new_index = state.change_slice_depth(index, value)
    if index != new_index:
        state.selected_slice = None
        
    state.to_file(filename, save_image_slices=False,
                  save_depth_map=False, save_input_image=False)
    return True


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Input({'type': 'slice-undo-backwards', 'index': ALL}, 'n_clicks'),
              Input({'type': 'slice-undo-forwards', 'index': ALL}, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
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
    state.to_file(filename, save_image_slices=False,
                  save_depth_map=False, save_input_image=False)

    return True


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output('gen-slice-output', 'children'),
              Input(C.STORE_GENERATE_SLICE, 'data'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              prevent_initial_call=True)
def generate_slices(ignored_data, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if state.depthMapData is None:
        raise PreventUpdate()

    state.image_slices, state.image_depths = generate_image_slices(
        np.array(state.imgData),
        state.depthMapData,
        state.imgThresholds,
        num_expand=EXPAND_MASK)
    state.image_slices_filenames = []

    print(f'Generated {len(state.image_slices)} image slices; saving to file')
    state.to_file(filename)

    return True, ""


@app.callback(Output(C.IMAGE, 'src'),
              Output({'type': 'slice', 'index': ALL}, 'className'),
              Output(C.TEXT_POSITIVE_PROMPT, 'value', allow_duplicate=True),
              Output(C.TEXT_NEGATIVE_PROMPT, 'value', allow_duplicate=True),
              Input({'type': 'slice', 'index': ALL}, 'n_clicks'),
              State({'type': 'slice', 'index': ALL}, 'id'),
              State({'type': 'slice', 'index': ALL}, 'src'),
              State({'type': 'slice', 'index': ALL}, 'className'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              prevent_initial_call=True)
def display_slice(n_clicks, id, src, classnames, filename):
    if filename is None or n_clicks is None or any(n_clicks) is False:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    index = ctx.triggered_id['index']

    # if we are already displaying the slice, then we should remove it
    positive_prompt = ""
    negative_prompt = ""
    if state.selected_slice != index:
        state.selected_slice = index
        result = state.serve_slice_image_composed(index)
        positive_prompt = state.positive_prompts[index]
        negative_prompt = state.negative_prompts[index]
    else:
        state.selected_slice = None
        result = state.serve_input_image()

    new_classnames = highlight_selected_element(
        classnames, state.selected_slice, HIGHLIGHT_COLOR)

    return result, new_classnames, positive_prompt, negative_prompt


@app.callback(Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.LOADING_GLTF, 'children', allow_duplicate=True),
              Input(C.BTN_UPSCALE_TEXTURES, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              running=[(Output(C.BTN_UPSCALE_TEXTURES, 'disabled'), True, False)],
              prevent_initial_call=True)
def upscale_texture(n_clicks, filename, logs):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    state.upscale_slices()

    logs.append("Upscaled textures for slices")

    return logs, ""


@app.callback(Output(C.DOWNLOAD_GLTF, 'data'),
              Output(C.LOADING_GLTF, 'children', allow_duplicate=True),
              Input(C.BTN_GLTF_EXPORT, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.SLIDER_CAMERA_DISTANCE, 'value'),
              State(C.SLIDER_MAX_DISTANCE, 'value'),
              State(C.SLIDER_FOCAL_LENGTH, 'value'),
              State(C.SLIDER_DISPLACEMENT, 'value'),
              running=[(Output(C.BTN_GLTF_EXPORT, 'disabled'), True, False)],
              prevent_initial_call=True
              )
def gltf_export(n_clicks, filename, camera_distance, max_distance, focal_length, displacement_scale):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    gltf_path = export_state_as_gltf(
        state, filename, camera_distance, max_distance, focal_length, displacement_scale)

    return dcc.send_file(gltf_path, filename='scene.gltf'), ""


# XXX - this and the callback above can be chained to avoid code duplication
@app.callback(Output('model-viewer', 'srcDoc', allow_duplicate=True),
              Output(C.LOADING_GLTF, 'children', allow_duplicate=True),
              Input(C.BTN_GLTF_CREATE , 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.SLIDER_CAMERA_DISTANCE, 'value'),
              State(C.SLIDER_MAX_DISTANCE, 'value'),
              State(C.SLIDER_FOCAL_LENGTH, 'value'),
              State(C.SLIDER_DISPLACEMENT, 'value'),
              State(C.DROPDOWN_DEPTH_MODEL, 'value'),
              running=[(Output(C.BTN_GLTF_CREATE, 'disabled'), True, False)],
              prevent_initial_call=True
              )
def gltf_create(
    n_clicks, filename,
    camera_distance, max_distance, focal_length,
    displacement_scale, model_name):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    state.depth_model_name = model_name

    export_state_as_gltf(
        state, filename,
        camera_distance, max_distance, focal_length,
        displacement_scale, model=model_name)

    return get_gltf_iframe(state.serve_model_file()), ""


def export_state_as_gltf(
    state, filename,
    camera_distance, max_distance, focal_length,
    displacement_scale, model='midas'):
    camera_matrix, card_corners_3d_list = setup_camera_and_cards(
        state.image_slices,
        state.image_depths, camera_distance, max_distance, focal_length)

    depth_filenames = []
    if displacement_scale > 0:
        for i, image in enumerate(state.image_slices):
            print(f"Generating depth map for slice {i}")
            depth_filename = state.depth_filename(i)
            if not depth_filename.exists():
                model = DepthEstimationModel(model=model)
                if model != state.depth_estimation_model:
                    state.depth_estimation_model = model
                depth_map = generate_depth_map(
                    image[:, :, :3], model=state.depth_estimation_model)
                depth_map = postprocess_depth_map(depth_map, image[:, :, 3], final_blur=50)
                Image.fromarray(depth_map).save(
                    depth_filename, compress_level=1)
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


@app.callback(Output(C.DOWNLOAD_IMAGE, 'data'),
              Input({'type': 'slice-info', 'index': ALL}, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              prevent_initial_call=True)
def download_image(n_clicks, filename):
    if filename is None or n_clicks is None or ctx.triggered_id is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    index = ctx.triggered_id['index']
    if n_clicks[index] is None:
        raise PreventUpdate()

    # print(n_clicks, index, ctx.triggered)

    image_path = state.image_slices_filenames[index]

    return dcc.send_file(image_path, Path(state.image_slices_filenames[index]).name)


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Input({'type': 'slice-upload', 'index': ALL}, 'contents'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
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


@app.callback(Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Output(C.ANIMATION_OUTPUT, 'children'),
              Input(C.BTN_EXPORT_ANIMATION, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.SLIDER_NUM_FRAMES, 'value'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def export_animation(n_clicks, filename, num_frames, logs):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    camera_distance = 100.0
    max_distance = 500.0
    focal_length = 100.0
    camera_matrix, card_corners_3d_list = setup_camera_and_cards(
        state.image_slices, state.image_depths, camera_distance, max_distance, focal_length)

    # Render the initial view
    camera_position = np.array([0, 0, -100], dtype=np.float32)
    render_image_sequence(
        filename,
        state.image_slices, card_corners_3d_list, camera_matrix, camera_position,
        num_frames=num_frames)

    logs.append(f"Exported {num_frames} frames to animation")

    return logs, ""


@app.callback(
    Output(C.INPUT_EXTERNAL_SERVER, 'value'),
    Input(C.STORE_RESTORE_STATE, 'data'),
    State(C.STORE_APPSTATE_FILENAME, 'data'),
    prevent_initial_call=True)
def update_external_server_address(value, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    return state.server_address


@app.callback(
    Output('model-viewer', 'srcDoc'),
    Input(C.STORE_RESTORE_STATE, 'data'),
    State(C.STORE_APPSTATE_FILENAME, 'data'),
    prevent_initial_call=True)
def update_model_viewer(value, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)

    iframe = get_gltf_iframe(state.serve_model_file())

    return iframe


@app.callback(Output(C.STORE_UPDATE_SLICE, 'data', allow_duplicate=True),
              Input(C.STORE_RESTORE_STATE, 'data'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              prevent_initial_call=True)
def restore_state_slices(value, filename):
    if filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    if len(state.image_slices) == 0:
        print("No image slices to restore")
        raise PreventUpdate()

    return True


@app.callback(Output(C.STORE_TRIGGER_UPDATE_DEPTHMAP, 'data', allow_duplicate=True),
              Input(C.STORE_RESTORE_STATE, 'data'),
              prevent_initial_call=True)
def restore_state_depthmap(value):
    return True


@app.callback(
    # XXX - generate depth-map via separate callback
    Output(C.STORE_APPSTATE_FILENAME, 'data'),
    Output(C.STORE_RESTORE_STATE, 'data'),
    Output(C.IMAGE, 'src', allow_duplicate=True),
    Output(C.STORE_UPDATE_THRESHOLD_CONTAINER, 'data', allow_duplicate=True),
    Output(C.SLIDER_NUM_SLICES, 'value'),
    Output(C.LOGS_DATA, 'data', allow_duplicate=True),
    Input(C.UPLOAD_STATE, 'contents'),
    State(C.LOGS_DATA, 'data'),
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


@app.callback(Output(C.LOGS_DATA, 'data', allow_duplicate=True),
              Input(C.BTN_SAVE_STATE, 'n_clicks'),
              State(C.STORE_APPSTATE_FILENAME, 'data'),
              State(C.LOGS_DATA, 'data'),
              prevent_initial_call=True)
def save_state(n_clicks, filename, logs):
    if n_clicks is None or filename is None:
        raise PreventUpdate()

    state = AppState.from_cache(filename)
    state.to_file(filename)

    logs.append(f"Saved state to {filename}")

    return logs


@app.callback(Output(C.STORE_CURRENT_TAB, 'data', allow_duplicate=True),
              Input({'type': 'tab-content-main', 'index': ALL}, 'className'),
              prevent_initial_call=True)
def update_current_tab(classnames):
    names = ['Mode', 'Segmentation', 'Inpainting', 'Export', 'Configuration']
    for i, classname in enumerate(classnames):
        if 'hidden' not in classname:
            return names[i]
    return no_update


if __name__ == '__main__':
    os.environ['DISABLE_TELEMETRY'] = 'YES'
    os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
    
    # parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8050)
    parser.add_argument('--prefetch-models', type=str, default=None,
                        help='Either "all" or "default"')
    args = parser.parse_args()
    
    if not serving.is_running_from_reloader():
        if args.prefetch_models in ['all', 'default']:
            print("Prefetching models")
            if args.prefetch_models == 'all':
                for model in [DepthEstimationModel, SegmentationModel, InpaintingModel]:
                    for model_name in model.MODELS:
                        model(model_name).load_model()
            else:
                DepthEstimationModel().load_model()
                SegmentationModel().load_model()
                InpaintingModel().load_model()
        elif args.prefetch_models is not None:
            print(f'Invalid prefetch models argument: {args.prefetch_models}; use "all" or "default"')
            exit(1)
    
    app.run_server(port=args.port, debug=True)