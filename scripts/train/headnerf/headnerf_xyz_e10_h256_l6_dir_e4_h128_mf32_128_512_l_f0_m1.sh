export CUDA_VISIBLE_DEVICES=0
python train.py \
    --config-name headnerf \
    data.dataset.preload_image=False \
    losses.mask=1 \
    losses.l1_bg=0 \
    losses.l1_torso=0 \
    losses.l1_face=0 \
    implicit_function.render_size=[128,128] \
    precache_rays=False \
    visualization.visdom_env='headnerf_xyz_e10_h256_l6_dir_e4_h128_mf32_64_512_l_f0_m1' \
    # test=null \
