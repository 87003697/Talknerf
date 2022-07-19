from einops import rearrange, repeat
import torch
import numpy as np
import math
import random

def my_get_world_points_and_direction(batch_size,
                                      num_steps,
                                      img_size,
                                      fov,
                                      ray_start,
                                      ray_end,
                                      h_stddev=None,
                                      v_stddev=None,
                                      h_mean=None,
                                      v_mean=None,
                                      sample_dist=None,
                                      lock_view_dependence=False,
                                      device='cpu',
                                      camera_pos=None,
                                      camera_lookup=None,
                                         c2w_matrix=None,
                                      **kwargs,
                                      ):
    """
    Generate sample points and camera rays in the world coordinate system.

    :param batch_size:
    :param num_steps: number of samples for each ray
    :param img_size:
    :param fov:
    :param ray_start:
    :param ray_end:
    :param h_stddev:
    :param v_stddev:
    :param h_mean:
    :param v_mean:
    :param sample_dist: mode for sample_camera_positions
    :param lock_view_dependence:
    :return:
    - transformed_points: (b, h x w x num_steps, 3), has been perturbed
    - transformed_ray_directions_expanded: (b, h x w x num_steps, 3)
    - transformed_ray_origins: (b, h x w, 3)
    - transformed_ray_directions: (b, h x w, 3)
    - z_vals: (b, h x w, num_steps, 1), has been perturbed
    - pitch: (b, 1)
    - yaw: (b, 1)
    """

    # Generate initial camera rays and sample points.
    # batch_size, pixels, num_steps, 1
    points_cam, z_vals, rays_d_cam = get_initial_rays_trig(
        bs=batch_size,
        num_steps=num_steps,
        resolution=(img_size, img_size),
        device=device,
        fov=fov,
        ray_start=ray_start,
        ray_end=ray_end)

    transformed_points, \
        z_vals, \
        transformed_ray_directions, \
        transformed_ray_origins, \
        = transform_sampled_points(points_cam,
                                              z_vals,
                                              rays_d_cam,
                                              h_stddev=h_stddev,
                                              v_stddev=v_stddev,
                                              h_mean=h_mean,
                                              v_mean=v_mean,
                                              device=device,
                                              mode=sample_dist,
                                              camera_pos=camera_pos,
                                              camera_lookup=camera_lookup,
                                              c2w_matrix=c2w_matrix,
                                              )

    transformed_ray_directions_expanded = repeat(
        transformed_ray_directions, "b hw xyz -> b (hw s) xyz", s=num_steps)
#   if lock_view_dependence:
#     transformed_ray_directions_expanded = torch.zeros_like(transformed_ray_directions_expanded)
#     transformed_ray_directions_expanded[..., -1] = -1

    transformed_points = rearrange(
        transformed_points, "b hw s xyz -> b (hw s) xyz")

    ret = (transformed_points, transformed_ray_directions_expanded,
           transformed_ray_origins, transformed_ray_directions, z_vals,
           )
    return ret

def get_initial_rays_trig(bs,
                          num_steps,
                          fov,
                          resolution,
                          ray_start,
                          ray_end,
                          device, ):
    """
    Returns sample points, z_vals, and ray directions in camera space.

    :param bs:
    :param num_steps: number of samples along a ray  # 积分长度?
    :param fov:
    :param resolution:
    :param ray_start:
    :param ray_end:
    :param device:
    :return:
    points: (b, HxW, n_samples, 3)
    z_vals: (b, HxW, n_samples, 1)
    rays_d_cam: (b, HxW, 3)

    """

    W, H = resolution
    # Create full screen NDC (-1 to +1) coords [x, y, 0, 1].
    # Y is flipped to follow image memory layouts.
    x, y = torch.meshgrid(torch.linspace(-1, 1, W, device=device),
                          torch.linspace(1, -1, H, device=device))
    x = x.T.flatten()  # (HxW, ) [[-1, ..., 1], ...]
    y = y.T.flatten()  # (HxW, ) [[1, ..., -1]^T, ...]
    z = -torch.ones_like(x, device=device) / \
        np.tan((2 * math.pi * fov / 360) / 2)  # (HxW, )

    rays_d_cam = normalize_vecs(torch.stack([x, y, z], -1))  # (HxW, 3)

    z_vals = torch.linspace(ray_start,
                            ray_end,
                            num_steps,
                            device=device) \
        .reshape(1, num_steps, 1) \
        .repeat(W * H, 1, 1)  # (HxW, n, 1)
    points = rays_d_cam.unsqueeze(1).repeat(
        1, num_steps, 1) * z_vals  # (HxW, n_samples, 3)

    points = torch.stack(bs * [points])  # (b, HxW, n_samples, 3)
    z_vals = torch.stack(bs * [z_vals])  # (b, HxW, n_samples, 1)
    rays_d_cam = torch.stack(bs * [rays_d_cam]).to(device)  # (b, HxW, 3)

    return points, z_vals, rays_d_cam

def normalize_vecs(vectors: torch.Tensor) -> torch.Tensor:
    """
    Normalize vector lengths.

    :param vectors:
    :return:
    """

    out = vectors / (torch.norm(vectors, dim=-1, keepdim=True))
    return out

def transform_sampled_points(points,
                             z_vals,
                             ray_directions,
                             device,
                             h_stddev=1,
                             v_stddev=1,
                             h_mean=math.pi * 0.5,
                             v_mean=math.pi * 0.5,
                             mode='normal',
                             camera_pos=None,
                             camera_lookup=None,
                             c2w_matrix=None,
                             ):
    """
    Perturb z_vals and points;
    Samples a camera position and maps points in camera space to world space.

    :param points: (bs, num_rays, n_samples, 3)
    :param z_vals: (bs, num_rays, n_samples, 1)
    :param ray_directions: (bs, num_rays, 3)
    :param device:
    :param h_stddev:
    :param v_stddev:
    :param h_mean:
    :param v_mean:
    :param mode: mode for sample_camera_positions
    :return:
    - transformed_points: (bs, num_rays, n_samples, 3)
    - z_vals: (bs, num_rays, n_samples, 1)
    - transformed_ray_directions: (bs, num_rays, 3)
    - transformed_ray_origins: (bs, num_rays, 3)
    - pitch: (bs, 1)
    - yaw: (bs, 1)
    """

    bs, num_rays, num_steps, channels = points.shape

    points, z_vals = perturb_points(points,
                                    z_vals,
                                    ray_directions,
                                    device)
    if c2w_matrix is not None:
        cam2world_matrix = c2w_matrix
    else:
        if camera_pos is None or camera_lookup is None:
            # (b, 3) (b, 1) (b, 1)
            camera_origin, pitch, yaw = sample_camera_positions(
                bs=bs,
                r=1,
                horizontal_stddev=h_stddev,
                vertical_stddev=v_stddev,
                horizontal_mean=h_mean,
                vertical_mean=v_mean,
                device=device,
                mode=mode)
            forward_vector = normalize_vecs(-camera_origin)  # (b, 3)
        else:
            # print(camera_pos.shape, camera_lookup.shape)
            camera_origin = camera_pos
            pitch = yaw = torch.zeros(bs, 1, device=device)
            forward_vector = normalize_vecs(camera_lookup)  # (b, 3)
    
        cam2world_matrix = create_cam2world_matrix(forward_vector,
                                                camera_origin,
                                                device=device)

    points_homogeneous = torch.ones(
        (points.shape[0], points.shape[1],
         points.shape[2], points.shape[3] + 1),
        device=device)
    points_homogeneous[:, :, :, :3] = points
    # print(points_homogeneous.shape)
    # print(cam2world_matrix.shape)
    # assert cam2world_matrix.shape[0] == points_homogeneous.shape[0]
    # (bs, 4, 4) @ (bs, 4, num_rays x n_samples) -> (bs, 4, num_rays x n_samples) -> (bs, num_rays, n_samples, 4)
    transformed_points = torch.bmm(
        cam2world_matrix,
        points_homogeneous.reshape(bs, -1, 4).permute(0, 2, 1)) \
        .permute(0, 2, 1) \
        .reshape(bs, num_rays, num_steps, 4)
    # (bs, num_rays, n_samples, 3)
    transformed_points = transformed_points[..., :3]

    # (bs, 3, 3) @ (bs, 3, num_rays) -> (bs, 3, num_rays) -> (bs, num_rays, 3)
    transformed_ray_directions = torch.bmm(
        cam2world_matrix[..., :3, :3],
        ray_directions.reshape(bs, -1, 3).permute(0, 2, 1)) \
        .permute(0, 2, 1) \
        .reshape(bs, num_rays, 3)

    homogeneous_origins = torch.zeros((bs, 4, num_rays), device=device)
    homogeneous_origins[:, 3, :] = 1
    # (bs, 4, 4) @ (bs, 4, num_rays) -> (bs, 4, num_rays) -> (bs, num_rays, 4)
    transformed_ray_origins = torch.bmm(
        cam2world_matrix,
        homogeneous_origins) \
        .permute(0, 2, 1) \
        .reshape(bs, num_rays, 4)
    # (bs, num_rays, 3)
    transformed_ray_origins = transformed_ray_origins[..., :3]

    return transformed_points, z_vals, transformed_ray_directions, transformed_ray_origins

def perturb_points(points,
                   z_vals,
                   ray_directions,
                   device):
    """
    Perturb z_vals and then points

    :param points: (n, num_rays, n_samples, 3)
    :param z_vals: (n, num_rays, n_samples, 1)
    :param ray_directions: (n, num_rays, 3)
    :param device:
    :return:
    points: (n, num_rays, n_samples, 3)
    z_vals: (n, num_rays, n_samples, 1)
    """
    distance_between_points = z_vals[:, :, 1:2, :] - \
        z_vals[:, :, 0:1, :]  # (n, num_rays, 1, 1)
    offset = (torch.rand(z_vals.shape, device=device) - 0.5) \
        * distance_between_points  # [-0.5, 0.5] * d, (n, num_rays, n_samples, 1)
    z_vals = z_vals + offset

    points = points + \
        offset * ray_directions.unsqueeze(2)  # (n, num_rays, n_samples, 3)
    return points, z_vals

def create_cam2world_matrix(forward_vector,
                            origin,
                            device=None):
    """
    Takes in the direction the camera is pointing
    and the camera origin and returns a cam2world matrix.

    :param forward_vector: (bs, 3), looking at direction
    :param origin: (bs, 3)
    :param device:
    :return:
    cam2world: (bs, 4, 4)
    """
    """"""

    forward_vector = normalize_vecs(forward_vector)
    up_vector = torch.tensor([0, 1, 0], dtype=torch.float, device=device) \
        .expand_as(forward_vector)

    left_vector = normalize_vecs(
        torch.cross(up_vector,
                    forward_vector,
                    dim=-1))

    up_vector = normalize_vecs(
        torch.cross(forward_vector,
                    left_vector,
                    dim=-1))

    rotation_matrix = torch.eye(4, device=device) \
        .unsqueeze(0) \
        .repeat(forward_vector.shape[0], 1, 1)
    rotation_matrix[:, :3, :3] = torch.stack(
        (-left_vector, up_vector, -forward_vector), axis=-1)

    translation_matrix = torch.eye(4, device=device) \
        .unsqueeze(0) \
        .repeat(forward_vector.shape[0], 1, 1)
    translation_matrix[:, :3, 3] = origin

    cam2world = translation_matrix @ rotation_matrix

    return cam2world

def sample_camera_positions(device,
                            bs=1,
                            r=1,
                            horizontal_stddev=1,
                            vertical_stddev=1,
                            horizontal_mean=math.pi * 0.5,
                            vertical_mean=math.pi * 0.5,
                            mode='normal'):
    """
    Samples bs random locations along a sphere of radius r. Uses the specified distribution.

    :param device:
    :param bs:
    :param r:
    :param horizontal_stddev: yaw std
    :param vertical_stddev: pitch std
    :param horizontal_mean:
    :param vertical_mean:
    :param mode:
    :return:
    output_points: (bs, 3), camera positions
    phi: (bs, 1), pitch in radians [0, pi]
    theta: (bs, 1), yaw in radians [-pi, pi]
    """

    if mode == 'uniform':
        theta = (torch.rand((bs, 1), device=device) - 0.5) \
            * 2 * horizontal_stddev \
            + horizontal_mean
        phi = (torch.rand((bs, 1), device=device) - 0.5) \
            * 2 * vertical_stddev \
            + vertical_mean

    elif mode == 'normal' or mode == 'gaussian':
        theta = torch.randn((bs, 1), device=device) \
            * horizontal_stddev \
            + horizontal_mean
        phi = torch.randn((bs, 1), device=device) \
            * vertical_stddev \
            + vertical_mean

    elif mode == 'hybrid':
        if random.random() < 0.5:
            theta = (torch.rand((bs, 1), device=device) - 0.5) \
                * 2 * horizontal_stddev * 2 \
                + horizontal_mean
            phi = (torch.rand((bs, 1), device=device) - 0.5) \
                * 2 * vertical_stddev * 2 \
                + vertical_mean
        else:
            theta = torch.randn((bs, 1), device=device) * \
                horizontal_stddev + horizontal_mean
            phi = torch.randn((bs, 1), device=device) * \
                vertical_stddev + vertical_mean

    elif mode == 'truncated_gaussian':
        theta = truncated_normal_(torch.zeros((bs, 1), device=device)) \
            * horizontal_stddev \
            + horizontal_mean
        phi = truncated_normal_(torch.zeros((bs, 1), device=device)) \
            * vertical_stddev \
            + vertical_mean

    elif mode == 'spherical_uniform':
        theta = (torch.rand((bs, 1), device=device) - .5) \
            * 2 * horizontal_stddev \
            + horizontal_mean
        v_stddev, v_mean = vertical_stddev / math.pi, vertical_mean / math.pi
        v = ((torch.rand((bs, 1), device=device) - .5) * 2 * v_stddev + v_mean)
        v = torch.clamp(v, 1e-5, 1 - 1e-5)
        phi = torch.arccos(1 - 2 * v)

    elif mode == 'mean':
        # Just use the mean.
        theta = torch.ones((bs, 1), device=device,
                           dtype=torch.float) * horizontal_mean
        phi = torch.ones((bs, 1), device=device,
                         dtype=torch.float) * vertical_mean
    else:
        assert 0

    phi = torch.clamp(phi, 1e-5, math.pi - 1e-5)

    output_points = torch.zeros((bs, 3), device=device)  # (bs, 3)
    output_points[:, 0:1] = r * torch.sin(phi) * torch.cos(theta)  # x
    output_points[:, 2:3] = r * torch.sin(phi) * torch.sin(theta)  # z
    output_points[:, 1:2] = r * torch.cos(phi)  # y

    return output_points, phi, theta

def truncated_normal_(tensor, mean=0, std=1):
    size = tensor.shape
    tmp = tensor.new_empty(size + (4,)).normal_()
    valid = (tmp < 2) & (tmp > -2)
    ind = valid.max(-1, keepdim=True)[1]
    tensor.data.copy_(tmp.gather(-1, ind).squeeze(-1))
    tensor.data.mul_(std).add_(mean)
    return tensor

def get_cam2world_matrix(angles, bs=None, device=torch.device('cpu'), random_range=(10, 30, 0)):
    # angles: bs,3;  if angles is None, 随机采样
    if angles is None:
        cam_x_angle = (torch.rand(size=(bs, 1), device=device) - 0.5) * 2 * random_range[0]
        cam_y_angle = (torch.rand(size=(bs, 1), device=device) - 0.5) * 2 * random_range[1]
        cam_z_angle = (torch.rand(size=(bs, 1), device=device) - 0.5) * 2 * random_range[2]
        # print(cam_x_angle, cam_y_angle, cam_z_angle)
        # exit()
        cam_x_angle = angel2radius(cam_x_angle)  # bs, 1
        cam_y_angle = angel2radius(cam_y_angle)
        cam_z_angle = angel2radius(cam_z_angle)
    else: # 测试阶段
        bs = angles.shape[0]
        angles = angel2radius(angles)  # bs, 3
        cam_x_angle = angles[:, 0:1]  # bs, 1
        cam_y_angle = angles[:, 1:2]
        cam_z_angle = angles[:, 2:]

    origin_pos = torch.zeros(size=(bs, 3), device=device)  # (bs, 3)
    face_center_pos = torch.tensor([0, 0, -1], device=device).unsqueeze(0).repeat(bs, 1) # bs, 3
    zeros_translation = torch.zeros(size=(bs, 3), device=device)  # (bs, 3)
    
    r_input = torch.cat([cam_x_angle, cam_y_angle, cam_z_angle], dim=1) # bs, 3
    r_input = torch.cat([r_input, zeros_translation], dim=-1) # bs, 6
    Rotation_mat = euler2mat(r_input)  # b, 4, 4
    # print(Rotation_mat.shape, face_center_pos.shape)
    homogeneous_face_center_pos = torch.ones(size=(bs, 4), device=device) # (bs, 4)
    homogeneous_face_center_pos[:, :3] = face_center_pos
    # print(Rotation_mat.shape, homogeneous_face_center_pos.shape) # bs, 4, 1
    trans_face_pos =  torch.bmm(Rotation_mat, homogeneous_face_center_pos.unsqueeze(2)).squeeze(2) # b,4
    trans_face_pos = trans_face_pos[:, :3] # b,3


    forward_vector = -trans_face_pos  # bs, 3
    # print((forward_vector ** 2).sum())
    forward_vector = normalize_vecs(forward_vector)
    # cam_pos = forward_vector + origin_pos

    r_input = torch.zeros(size=(bs, 3), device=device)  # 不旋转
    r_input = torch.cat([r_input, forward_vector], dim=1)  # bs, 6
    Trans_mat = euler2mat(r_input)
    total_mat = Trans_mat @ Rotation_mat  # b, 4, 4
    return total_mat

def angel2radius(v):
    return v / 180.0 * math.pi

def euler2mat(angle):
    # copy from video auto encoder
    """Convert euler angles to rotation matrix.
     Reference: https://github.com/pulkitag/pycaffe-utils/blob/master/rot_utils.py#L174
    Args:
        angle: rotation angle along 3 axis (a, b, y) in radians -- size = [B, 6], 后三个是平移向量
    Returns:
        Rotation matrix corresponding to the euler angles -- size = [B, 3, 3]
    """
    B = angle.size(0)
    device = angle.device

    x, y, z = angle[:, 0], angle[:, 1], angle[:, 2]

    cosz = torch.cos(z) # b,
    sinz = torch.sin(z)

    zeros = z.detach() * 0
    ones = zeros.detach() + 1
    zmat = torch.stack([cosz, -sinz, zeros,
                        sinz, cosz, zeros,
                        zeros, zeros, ones], dim=1).reshape(B, 3, 3)

    cosy = torch.cos(y)
    siny = torch.sin(y)

    ymat = torch.stack([cosy, zeros, siny,
                        zeros, ones, zeros,
                        -siny, zeros, cosy], dim=1).reshape(B, 3, 3)

    cosx = torch.cos(x)
    sinx = torch.sin(x)

    xmat = torch.stack([ones, zeros, zeros,
                        zeros, cosx, -sinx,
                        zeros, sinx, cosx], dim=1).reshape(B, 3, 3)
    rotMat = xmat @ ymat @ zmat
    v_trans = angle[:,3:]  # b,3
    rotMat = torch.cat([rotMat, v_trans.view([B, 3, 1])], 2) # b,3,4  # F.affine_grid takes 3x4
    total_mat = torch.eye(4, device=device).unsqueeze(0).repeat(B, 1, 1) # b,4,4
    total_mat[:, :3,:] = rotMat
    return total_mat