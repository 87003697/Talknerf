import torch
import cv2
import numpy
import math
from typing import Tuple
from pytorch3d.renderer import PerspectiveCameras, look_at_view_transform
from torch.utils.data.dataset import Dataset
from skimage.metrics import structural_similarity as ssim

def calc_lpips(x: torch.Tensor, y: torch.Tensor, func: torch.nn.Module):
    """
    Calculate the Learned Perceptual Image Patch Similarity between image 'x' and 'y'
    func: the trained perceptual model
    """
    return func(x, y)

def calc_ssim(x: numpy.array, y: numpy.array ):
    """
    Calculate the Structural Similarity between image 'x' and 'y'
    """
    gray_x = cv2.cvtColor(x, cv2.COLOR_RGB2GRAY)
    gray_y = cv2.cvtColor(y, cv2.COLOR_RGB2GRAY)
    return ssim(gray_x, gray_y)
    

def calc_mse(x: torch.Tensor, y: torch.Tensor):
    """
    Calculates the mean square error between tensors `x` and `y`.
    """
    return torch.mean((x - y) ** 2)


def calc_psnr(x: torch.Tensor, y: torch.Tensor):
    """
    Calculates the Peak-signal-to-noise ratio between tensors `x` and `y`.
    """
    mse = calc_mse(x, y)
    psnr = -10.0 * torch.log10(mse)
    return psnr


def sample_images_at_mc_locs(
    target_images: torch.Tensor,
    sampled_rays_xy: torch.Tensor,
):
    """
    Given a set of pixel locations `sampled_rays_xy` this method samples the tensor
    `target_images` at the respective 2D locations.

    This function is used in order to extract the colors from ground truth images
    that correspond to the colors rendered using a Monte Carlo rendering.

    Args:
        target_images: A tensor of shape `(batch_size, ..., 3)`.
        sampled_rays_xy: A tensor of shape `(batch_size, S_1, ..., S_N, 2)`.

    Returns:
        images_sampled: A tensor of shape `(batch_size, S_1, ..., S_N, 3)`
            containing `target_images` sampled at `sampled_rays_xy`.
    """
    ba = target_images.shape[0]
    dim = target_images.shape[-1]
    spatial_size = sampled_rays_xy.shape[1:-1]

    # The coordinate grid convention for grid_sample has both x and y
    # directions inverted.
    xy_sample = -sampled_rays_xy.view(ba, -1, 1, 2).clone()
    
    images_sampled = torch.nn.functional.grid_sample(
        target_images.permute(0, 3, 1, 2),
        xy_sample,
        align_corners=True,
        mode="bilinear",
    )
    return images_sampled.permute(0, 2, 3, 1).view(ba, *spatial_size, dim)

def sample_images_at_int_locs(
    target_images: torch.Tensor,
    sampled_rays_xy: torch.Tensor,
):
    """
    Given a set of pixel locations `sampled_rays_xy` this method samples the tensor
    `target_images` at the respective 2D locations.

    Unlike previous sample_images_at_mc_locs, here the sampled_rays should review the 
        real integar positions of pixels on the images, which means, it should range from 
        (0 ~ image_width, 0 ~ image_height)
    Args:
        target_images: A tensor of shape `(batch_size, ..., 3)`.
        sampled_rays_xy: A tensor of shape `(batch_size, S_1, ..., S_N, 2)`.

    Returns:
        images_sampled: A tensor of shape `(batch_size, S_1, ..., S_N, 3)`
            containing `target_images` sampled at `sampled_rays_xy`.
    """
    ba = target_images.shape[0]
    dim = target_images.shape[-1]
    spatial_size = sampled_rays_xy.shape[1:-1]

    # The coordinate grid convention for grid_sample has both x and y
    # directions inverted.
    
    gt_list = []
    pixels = sampled_rays_xy.type(torch.LongTensor)
    for idx in range(ba):
        gt_pixels = target_images[idx, pixels[idx,:,0], pixels[idx,:,1]]
        gt_list.append(gt_pixels[None])

    return torch.cat(gt_list, dim = 0)


def generate_eval_video_cameras(
    train_dataset,
    n_eval_cams: int = 100,
    trajectory_type: str = "figure_eight",
    trajectory_scale: float = 0.2,
    scene_center: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    up: Tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> Dataset[torch.Tensor]:
    """
    Generate a camera trajectory for visualizing a NeRF model.

    Args:
        train_dataset: The training dataset object.
        n_eval_cams: Number of cameras in the trajectory.
        trajectory_type: The type of the camera trajectory. Can be one of:
            circular: Rotating around the center of the scene at a fixed radius.
            figure_eight: Figure-of-8 trajectory around the center of the
                central camera of the training dataset.
            trefoil_knot: Same as 'figure_eight', but the trajectory has a shape
                of a trefoil knot (https://en.wikipedia.org/wiki/Trefoil_knot).
            figure_eight_knot: Same as 'figure_eight', but the trajectory has a shape
                of a figure-eight knot
                (https://en.wikipedia.org/wiki/Figure-eight_knot_(mathematics)).
        trajectory_scale: The extent of the trajectory.
        up: The "up" vector of the scene (=the normal of the scene floor).
            Active for the `trajectory_type="circular"`.
        scene_center: The center of the scene in world coordinates which all
            the cameras from the generated trajectory look at.
    Returns:
        Dictionary of camera instances which can be used as the test dataset
    """
    if trajectory_type in ("figure_eight", "trefoil_knot", "figure_eight_knot"):
        cam_centers = torch.cat(
            [e["camera"].get_camera_center() for e in train_dataset]
        )
        # get the nearest camera center to the mean of centers
        mean_camera_idx = (
            ((cam_centers - cam_centers.mean(dim=0)[None]) ** 2)
            .sum(dim=1)
            .min(dim=0)
            .indices
        )
        # generate the knot trajectory in canonical coords
        time = torch.linspace(0, 2 * math.pi, n_eval_cams + 1)[:n_eval_cams]
        if trajectory_type == "trefoil_knot":
            traj = _trefoil_knot(time)
        elif trajectory_type == "figure_eight_knot":
            traj = _figure_eight_knot(time)
        elif trajectory_type == "figure_eight":
            traj = _figure_eight(time)
        traj[:, 2] -= traj[:, 2].max()

        # transform the canonical knot to the coord frame of the mean camera
        traj_trans = (
            train_dataset[mean_camera_idx]["camera"]
            .get_world_to_view_transform()
            .inverse()
        )
        traj_trans = traj_trans.scale(cam_centers.std(dim=0).mean() * trajectory_scale)
        traj = traj_trans.transform_points(traj)

    elif trajectory_type == "circular":
        cam_centers = torch.cat(
            [e["camera"].get_camera_center() for e in train_dataset]
        )

        # fit plane to the camera centers
        plane_mean = cam_centers.mean(dim=0)
        cam_centers_c = cam_centers - plane_mean[None]

        if up is not None:
            # us the up vector instead of the plane through the camera centers
            plane_normal = torch.FloatTensor(up)
        else:
            cov = (cam_centers_c.t() @ cam_centers_c) / cam_centers_c.shape[0]
            _, e_vec = torch.symeig(cov, eigenvectors=True)
            plane_normal = e_vec[:, 0]

        plane_dist = (plane_normal[None] * cam_centers_c).sum(dim=-1)
        cam_centers_on_plane = cam_centers_c - plane_dist[:, None] * plane_normal[None]

        cov = (
            cam_centers_on_plane.t() @ cam_centers_on_plane
        ) / cam_centers_on_plane.shape[0]
        _, e_vec = torch.symeig(cov, eigenvectors=True)
        traj_radius = (cam_centers_on_plane ** 2).sum(dim=1).sqrt().mean()
        angle = torch.linspace(0, 2.0 * math.pi, n_eval_cams)
        traj = traj_radius * torch.stack(
            (torch.zeros_like(angle), angle.cos(), angle.sin()), dim=-1
        )
        traj = traj @ e_vec.t() + plane_mean[None]

    else:
        raise ValueError(f"Unknown trajectory_type {trajectory_type}.")

    # point all cameras towards the center of the scene
    R, T = look_at_view_transform(
        eye=traj,
        at=(scene_center,),  # (1, 3)
        up=(up,),  # (1, 3)
        device=traj.device,
    )

    # get the average focal length and principal point
    focal = torch.cat([e["camera"].focal_length for e in train_dataset]).mean(dim=0)
    p0 = torch.cat([e["camera"].principal_point for e in train_dataset]).mean(dim=0)

    # assemble the dataset
    test_dataset = [
        {
            "image": None,
            "camera": PerspectiveCameras(
                focal_length=focal[None],
                principal_point=p0[None],
                R=R_[None],
                T=T_[None],
            ),
            "camera_idx": i,
        }
        for i, (R_, T_) in enumerate(zip(R, T))
    ]

    return test_dataset


def _figure_eight_knot(t: torch.Tensor, z_scale: float = 0.5):
    x = (2 + (2 * t).cos()) * (3 * t).cos()
    y = (2 + (2 * t).cos()) * (3 * t).sin()
    z = (4 * t).sin() * z_scale
    return torch.stack((x, y, z), dim=-1)


def _trefoil_knot(t: torch.Tensor, z_scale: float = 0.5):
    x = t.sin() + 2 * (2 * t).sin()
    y = t.cos() - 2 * (2 * t).cos()
    z = -(3 * t).sin() * z_scale
    return torch.stack((x, y, z), dim=-1)


def _figure_eight(t: torch.Tensor, z_scale: float = 0.5):
    x = t.cos()
    y = (2 * t).sin() / 2
    z = t.sin() * z_scale
    return torch.stack((x, y, z), dim=-1)
