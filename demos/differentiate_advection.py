""" Differentiate through Advection

This application demonstrates the backpropagation through an advection operation.

The demo Optimizes a velocity field so that a passively advected marker quantity matches the specified target density after advection.
"""
from phi.torch.flow import *
# from phi.jax.flow import *
# from phi.tf.flow import *


DOMAIN = dict(x=50, y=50, bounds=Box[0:100, 0:100])
MARKER_0 = CenteredGrid(Sphere((40, 50), radius=20), extrapolation.BOUNDARY, **DOMAIN)
MARKER_TARGET = CenteredGrid(Sphere((60, 50), radius=20), extrapolation.BOUNDARY, **DOMAIN)


def loss(velocity):
    advected = advect.mac_cormack(MARKER_0, velocity, dt=1.0)
    smooth_diff = diffuse.explicit(advected - MARKER_TARGET, 10.0, 1, 50)
    return field.l2_loss(smooth_diff), advected, smooth_diff


gradient_function = field.functional_gradient(loss, get_output=True)

velocity_fit = StaggeredGrid(0, extrapolation.ZERO, **DOMAIN)
marker_fit = CenteredGrid(0, extrapolation.BOUNDARY, **DOMAIN)
smooth_difference = CenteredGrid(0, extrapolation.BOUNDARY, **DOMAIN)
app = view(display=['marker_fit', 'gradient'], play=False)

for iteration in app.range(warmup=1):
    (loss, marker_fit, smooth_difference), (gradient,) = gradient_function(velocity_fit)
    app.info(f"Loss = {loss:.2f}")
    app.log_scalars(loss=loss)
    velocity_fit -= gradient
