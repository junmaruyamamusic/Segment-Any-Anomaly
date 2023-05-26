import argparse
import os
import copy

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

# Grounding DINO
import GroundingDINO.groundingdino.datasets.transforms as T
from GroundingDINO.groundingdino.models import build_model
from GroundingDINO.groundingdino.util import box_ops
from GroundingDINO.groundingdino.util.slconfig import SLConfig
from GroundingDINO.groundingdino.util.utils import clean_state_dict, get_phrases_from_posmap

# segment anything
from SAM.segment_anything import build_sam, SamPredictor
import cv2
import numpy as np
import matplotlib.pyplot as plt


def load_image(image_path):
    # load image
    image_pil = Image.open(image_path).convert("RGB")  # load image

    transform = T.Compose(
        [
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    image, _ = transform(image_pil, None)  # 3, h, w
    return image_pil, image


def load_model(model_config_path, model_checkpoint_path, device):
    args = SLConfig.fromfile(model_config_path)
    args.device = device
    model = build_model(args)
    checkpoint = torch.load(model_checkpoint_path, map_location="cpu")
    load_res = model.load_state_dict(clean_state_dict(checkpoint["model"]), strict=False)
    print(load_res)
    _ = model.eval()
    return model


def get_grounding_output(model, image, caption, box_threshold, text_threshold, category, with_logits=True, device="cpu", area_thr=0.8):
    caption = caption.lower()
    caption = caption.strip()
    if not caption.endswith("."):
        caption = caption + "."
    model = model.to(device)
    image = image.to(device)
    with torch.no_grad():
        outputs = model(image[None], captions=[caption])
    logits = outputs["pred_logits"].cpu().sigmoid()[0]  # (nq, 256)
    boxes = outputs["pred_boxes"].cpu()[0]  # (nq, 4)

    # filter output
    logits_filt = logits.clone()
    boxes_filt = boxes.clone()
    boxes_area = boxes_filt[:, 2] * boxes_filt[:, 3]
    filt_mask = torch.bitwise_and((logits_filt.max(dim=1)[0] > box_threshold), (boxes_area < area_thr))

    if torch.sum(filt_mask) == 0: # in case there are no matches
        filt_mask = torch.argmax(logits_filt.max(dim=1)[0])
        logits_filt = logits_filt[filt_mask].unsqueeze(0)  # num_filt, 256
        boxes_filt = boxes_filt[filt_mask].unsqueeze(0)
    else:
        logits_filt = logits_filt[filt_mask]  # num_filt, 256
        boxes_filt = boxes_filt[filt_mask]  # num_filt, 4

    # get phrase
    tokenlizer = model.tokenizer
    tokenized = tokenlizer(caption)

    # build pred
    pred_phrases = []
    boxes_filt_category = []
    for logit, box in zip(logits_filt, boxes_filt):
        pred_phrase = get_phrases_from_posmap(logit > text_threshold, tokenized, tokenlizer)
        if pred_phrase.count(category) > 0: # we don't want to predict the category
            continue

        if with_logits:
            pred_phrases.append(pred_phrase + f"({str(logit.max().item())[:4]})")
        else:
            pred_phrases.append(pred_phrase)
        boxes_filt_category.append(box)
    boxes_filt_category = torch.stack(boxes_filt_category, dim=0)

    return boxes_filt_category, pred_phrases

def show_mask(mask, ax, random_color=True):
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([30/255, 144/255, 255/255, 0.6])
    h, w = mask.shape[-2:]
    mask_image = mask.reshape(h, w, 1) * color.reshape(1, 1, -1)
    ax.imshow(mask_image)


def show_box(box, ax, label):
    x0, y0 = box[0], box[1]
    w, h = box[2] - box[0], box[3] - box[1]
    ax.add_patch(plt.Rectangle((x0, y0), w, h, edgecolor='green', facecolor=(0,0,0,0), lw=2)) 
    ax.text(x0, y0, label)


if __name__ == "__main__":

    parser = argparse.ArgumentParser("Grounded-Segment-Anything Demo", add_help=True)
    parser.add_argument("--config", type=str, default='GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py', help="path to config file")
    parser.add_argument(
        "--grounded_checkpoint", type=str, default='weights/groundingdino_swint_ogc.pth', help="path to checkpoint file"
    )
    parser.add_argument(
        "--sam_checkpoint", type=str, default='weights/sam_vit_h_4b8939.pth', help="path to checkpoint file"
    )
    parser.add_argument("--input_image", type=str, nargs='+', default=['cable.jpg'], help="input images")  # 複数の画像を受け取るように修正
    parser.add_argument("--category", type=str, default=['cable'])
    parser.add_argument("--text_prompt", type=str, default=['the black hole on the cable'], help="text prompt")
    parser.add_argument(
        "--output_dir", "-o", type=str, default="outputs", help="output directory"
    )

    parser.add_argument("--box_threshold", type=float, default=0.2, help="box threshold")
    parser.add_argument("--text_threshold", type=float, default=0.2, help="text threshold")
    parser.add_argument("--area_threshold", type=float, default=0.9, help="defect area threshold")

    parser.add_argument("--device", type=str, default="cuda", help="running on cpu only!, default=False")
    args = parser.parse_args()

    # cfg
    config_file = args.config  # change the path of the model config file
    grounded_checkpoint = args.grounded_checkpoint  # change the path of the model
    sam_checkpoint = args.sam_checkpoint
    image_paths = args.input_image  # image_pathsをリストとして受け取る
    text_prompts = args.text_prompt
    output_dir = args.output_dir
    box_threshold = args.box_threshold
    text_threshold = args.box_threshold
    area_threshold = args.area_threshold
    categories = args.category
    device = args.device

    # print(text_threshold)
    # make dir
    os.makedirs(output_dir, exist_ok=True)
    # load image
    image_pil, image = load_image(image_path)
    # load model
    model = load_model(config_file, grounded_checkpoint, device=device)

    # visualize raw image
    image_pil.save(os.path.join(output_dir, "raw_image.jpg"))

    # run grounding dino model for each category, text_prompt, and image
    for category, text_prompt, image_path in zip(categories, text_prompts, image_paths):  # 各カテゴリー、プロンプト、画像の組み合わせに対してループ
        # Load image
        image = Image.open(image_path).convert("RGB")
        # Grounding DINO model
        boxes_filt, pred_phrases = get_grounding_output(
            model, image, text_prompt, box_threshold, text_threshold, category=category, device=device, area_thr=area_threshold
        )

        # initialize SAM
        predictor = SamPredictor(build_sam(checkpoint=sam_checkpoint))
        image = cv2.imread(image_path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        predictor.set_image(image)

        size = image.size
        H, W = size[1], size[0]
        for i in range(boxes_filt.size(0)):
            boxes_filt[i] = boxes_filt[i] * torch.Tensor([W, H, W, H])
            boxes_filt[i][:2] -= boxes_filt[i][2:] / 2
            boxes_filt[i][2:] += boxes_filt[i][:2]

        boxes_filt = boxes_filt.cpu()
        transformed_boxes = predictor.transform.apply_boxes_torch(boxes_filt, image.shape[:2])

        masks, _, _ = predictor.predict_torch(
            point_coords = None,
            point_labels = None,
            boxes = transformed_boxes,
            multimask_output = False,
        )

        # draw output image
        plt.figure(figsize=(10, 10))
        plt.imshow(image)
        for mask in masks:
            show_mask(mask.cpu().numpy(), plt.gca(), random_color=True)
        for box, label in zip(boxes_filt, pred_phrases):
            show_box(box.numpy(), plt.gca(), label)
        plt.axis('off')
        plt.savefig(os.path.join(output_dir, "grounded_sam_output_{}.jpg".format(image_path)), bbox_inches="tight")  # output file name is unique for each image
