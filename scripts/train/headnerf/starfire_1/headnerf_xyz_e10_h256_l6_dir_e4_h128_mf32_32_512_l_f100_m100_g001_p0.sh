export CUDA_VISIBLE_DEVICES=0
python train.py \
    --config-name headnerf \
    data.dataset.preload_image=False \
    losses.mask=100 \
    losses.l1_bg=0 \
    losses.l1_torso=0 \
    losses.l1_face=100 \
    losses.gan=0.01 \
    losses.percept=0 \
    implicit_function.render_size=[32,32] \
    precache_rays=False \
    visualization.visdom_env='headnerf_xyz_e10_h256_l6_dir_e4_h128_mf32_32_512_l_f100_m100_g001_p0' \
    # test=null \
