# Copyright 2019 NREL

# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

import numpy as np
import matplotlib.pyplot as plt
from scipy.sparse import diags
from scipy.sparse.linalg import cg
#from pyamg import smoothed_aggregation_solver
#from pyamg.gallery import poisson

DEBUG = True

class PressureField(object):
    """
    Given a modeled velocity field, solve for the perturbation pressure
    field corresponding to a perturbation velocity field that, when
    combined with the original velocity field, satisfies mass
    conservation.
    """
    def __init__(self,flow_field):
        # setup grid
        self.x = flow_field.x
        self.y = flow_field.y
        self.z = flow_field.z
        self.Nx, self.Ny, self.Nz = self.x.shape
        self.N = self.Nx * self.Ny * self.Nz

        # get actual grid spacings
        self.x1 = self.x[:,0,0]
        self.y1 = self.y[0,:,0]
        self.z1 = self.z[0,0,:]
        dx = np.diff(self.x1)
        dy = np.diff(self.y1)
        dz = np.diff(self.z1)
        assert np.max(np.abs(dx - dx[0]) < 1e-8)
        assert np.max(np.abs(dy - dy[0]) < 1e-8)
        assert np.max(np.abs(dz - dz[0]) < 1e-8)
        self.dx = dx[0]
        self.dy = dy[0]
        self.dz = dz[0]

        # setup solver matrices
        self._setup_LHS()
        self.RHS = None

        #--------------------------------------------------------------
        if DEBUG:
            self.Nsolves = 0
            self.khub = np.argmin(np.abs(self.z[0,0,:] - 90.))
            print('Initialized PressureField',self.x.shape)

    def _setup_LHS(self):
        """Compressed sparse row (CSR) format appears to be slightly
        more efficient than compressed sparse column (CSC).
        """
        ones = np.ones((self.N,))
        diag = -2*ones/self.dx**2 - 2*ones/self.dy**2 - 2*ones/self.dz**2
        # off diagonal for d/dx operator    
        offx = self.Ny*self.Nz
        offdiagx = ones[:-offx]/self.dx**2
        # off diagonal for d/dy operator    
        offy = self.Nz
        offdiagy = ones[:-offy]/self.dy**2
        for i in range(offx, len(offdiagy), offx):
            offdiagy[i-offy:i] -= 1./self.dy**2
        # off diagonal for d/dz operator    
        offz = 1
        offdiagz = ones[:-offz]/self.dz**2
        offdiagz[self.Nz-1::self.Nz] -= 1./self.dz**2
        # spsolve requires matrix to be in CSC or CSR format
        self.LHS = diags(
            [
                offdiagx,
                offdiagy,
                offdiagz,
                diag,
                offdiagz,
                offdiagy,
                offdiagx,
            ],
            [-offx,-offy,-offz,0,offz,offy,offx],
            format='csr'
        )

    def update_RHS(self):
        du0_dx, dv0_dy, dw0_dz = self._calc_gradients()
        self._set_RHS(du0_dx, dv0_dy, dw0_dz)

    def _calc_gradients(self,u=None,v=None,w=None):
        """Calculate RHS of Poisson equation, div(U), from finite
        differences. Second-order central differences are evaluated
        on the interior, first-order one-sided differences on the
        boundaries.
        """
        if u is None:
            u = self.u0
        if v is None:
            v = self.v0
        if w is None:
            w = self.w0
        du0_dx = np.zeros(self.u0.shape)
        dv0_dy = np.zeros(self.u0.shape)
        dw0_dz = np.zeros(self.u0.shape)
        # u, inlet
        du0_dx[0,:,:] = (u[1,:,:] - u[0,:,:]) / self.dx
        # u, outlet
        du0_dx[-1,:,:] = (u[-1,:,:] - u[-2,:,:]) / self.dx
        # interior
        du0_dx[1:-1,:,:] = (u[2:,:,:] - u[:-2,:,:]) / (2*self.dx)
        if v is not None:
            # v, -y
            dv0_dy[:,0,:] = (v[:,1,:] - v[:,0,:]) / self.dy
            # v, +y
            dv0_dy[:,-1,:] = (v[:,-1,:] - v[:,-2,:]) / self.dy
            # interior
            dv0_dy[:,1:-1,:] = (v[:,2:,:] - v[:,:-2,:]) / (2*self.dy)
        if w is not None:
            # w, lower
            dw0_dz[:,:,0] = (w[:,:,1] - w[:,:,0]) / self.dz
            # w, upper
            dw0_dz[:,:,-1] = (w[:,:,-1] - w[:,:,-2]) / self.dz
            # interior
            dw0_dz[:,:,1:-1] = (w[:,:,2:] - w[:,:,:-2]) / (2*self.dz)
        return du0_dx, dv0_dy, dw0_dz

    def _set_RHS(self,du_dx,dv_dy=None,dw_dz=None):
        """Set the RHS of the Poisson equation, which is the divergence
        of the initial velocity predictor (i.e., the modeled velocity
        field).
        """
        div = du_dx
        if dv_dy is not None:
            div += dv_dy
        if dw_dz is not None:
            div += dw_dz
        self.RHS = div.ravel()

    def _correct_fields(self,A=1.0):
        # central difference
        dp_dx = (self.p[2:,:,:] - self.p[:-2,:,:]) / (2*self.dx)
        dp_dy = (self.p[:,2:,:] - self.p[:,:-2,:]) / (2*self.dy)
        dp_dz = (self.p[:,:,2:] - self.p[:,:,:-2]) / (2*self.dz)

        # update velocity fields
        self.u = self.u0.copy()
        self.v = self.v0.copy()
        self.w = self.w0.copy()
        self.u[1:-1,:,:] -= A*dp_dx
        self.v[:,1:-1,:] -= A*dp_dy
        self.w[:,:,1:-1] -= A*dp_dz

    def div(self, corrected=False):
        if corrected:
            return sum(self._calc_gradients(self.u,self.v,self.w))
        else:
            return self.RHS.reshape(self.u0.shape)

    def solve(self, u_wake, v_wake=None, w_wake=None,
              smooth_disk_region=None,
              A=1.0, tol=1e-8):
        """Solve Poisson equation for perturbation pressure field,
        according to the formulation in Tannehill, Anderson, and Pletcher
        (1997).

        If smooth_disk_region option is set to a turbine object, then
        an extra interpolation step is performed to smooth the velocity
        field just upstream and downstream of the rotor disk.
        
        Note: For a fictitious timestep (dt), A == dt/rho [m^3-s/kg]
        """
        # set modeled/predictor fields
        self.u0 = u_wake.copy()
        if v_wake is None:
            self.v0 = np.zeros(self.u0.shape)
        else:
            self.v0 = v_wake.copy()
        if w_wake is None:
            self.w0 = np.zeros(self.u0.shape)
        else:
            self.w0 = w_wake.copy()

        # calculate gradients setup RHS
        self.update_RHS()

        # now solve
        soln = cg(self.LHS, self.RHS/A, x0=np.zeros((self.N,)), tol=tol, atol=tol)
        assert (soln[1] == 0) # success
        self.p = soln[0].reshape(self.u0.shape)
        self._correct_fields(A)

        # optional smoothing step
        if smooth_disk_region is not None:
            self.interp(**smooth_disk_region)

        #--------------------------------------------------------------
        if DEBUG:
            fig,ax = plt.subplots(nrows=2,figsize=(11,8))
            cmsh = ax[0].pcolormesh(self.x[:,:,self.khub],
                                    self.y[:,:,self.khub], 
                                    self.u0[:,:,self.khub], 
                                    cmap='coolwarm')
            fig.colorbar(cmsh,ax=ax[0])
            cmsh = ax[1].pcolormesh(self.x[:,:,self.khub],
                                    self.y[:,:,self.khub], 
                                    self.u[:,:,self.khub], 
                                    cmap='coolwarm')
            fig.colorbar(cmsh,ax=ax[1])
            fig.savefig('/var/tmp/u_from_psolve_{:04d}.png'.format(self.Nsolves))
            plt.close(fig)

            fig,ax = plt.subplots(nrows=2,figsize=(11,8))
            cmsh = ax[0].pcolormesh(self.x[:,:,self.khub],
                                    self.y[:,:,self.khub], 
                                    self.v0[:,:,self.khub], 
                                    cmap='coolwarm')
            fig.colorbar(cmsh,ax=ax[0])
            cmsh = ax[1].pcolormesh(self.x[:,:,self.khub],
                                    self.y[:,:,self.khub], 
                                    self.v[:,:,self.khub], 
                                    cmap='coolwarm')
            fig.colorbar(cmsh,ax=ax[1])
            fig.savefig('/var/tmp/v_from_psolve_{:04d}.png'.format(self.Nsolves))
            plt.close(fig)

            fig,ax = plt.subplots(nrows=2,figsize=(11,8))
            cmsh = ax[0].pcolormesh(self.x[:,:,self.khub],
                                    self.y[:,:,self.khub], 
                                    np.abs(self.div(corrected=False)[:,:,self.khub]),
                                    cmap='Reds')
            fig.colorbar(cmsh,ax=ax[0])
            cmsh = ax[1].pcolormesh(self.x[:,:,self.khub],
                                    self.y[:,:,self.khub], 
                                    np.abs(self.div(corrected=True)[:,:,self.khub]),
                                    cmap='Reds')
            fig.colorbar(cmsh,ax=ax[1])
            fig.savefig('/var/tmp/cont_err_from_psolve_{:04d}.png'.format(self.Nsolves))
            plt.close(fig)

            self.Nsolves += 1
            print('pressure solver count',self.Nsolves)

        return self.u, self.v, self.w

    def interp(self, x=None, y=None, z=None, coords=None, D=None, zhub=None):
        turbx = coords.x1
        turby = coords.x2
        turbz = coords.x3
        x1 = x[:,0,0]
        i0 = np.argmin(np.abs(turbx - x1))
#        if DEBUG:
#            print('interpolating region around rotor at',turbx,turby,turbz)
#        if x1[i0] >= turbx:
#            i0 -= 1
#        in_rotor_region = np.where(
#                ((turby - y)**2 + (turbz - zhub - z)**2) <= D/2)
#        def interpfun(f):
#            f0 = f[i0-1,in_rotor_region[1],in_rotor_region[2]]
#            f1 = f[i0+2,in_rotor_region[1],in_rotor_region[2]]
#            f[i0,in_rotor_region[1],in_rotor_region[2]] = 1*(f1-f0)/3 + f0
#            f[i0+1,in_rotor_region[1],in_rotor_region[2]] = 2*(f1-f0)/3 + f0
#            return f
#        self.u = interpfun(self.u)
        if DEBUG:
            print('interpolating yz-planes near rotor at',turbx,turby,turbz)
        if x1[i0] >= turbx:
            i0 -= 1
        f0 = self.u[i0-1,:,:]
        f1 = self.u[i0+2,:,:]
        self.u[i0,:] = 1*(f1-f0)/3 + f0
        self.u[i0+1,:] = 2*(f1-f0)/3 + f0

