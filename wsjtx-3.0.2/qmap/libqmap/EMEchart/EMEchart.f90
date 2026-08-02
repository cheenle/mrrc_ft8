program EMEchart

  ! Working together with 'gnuplot', this program produces annual
  ! Moon Ephemeris Charts useful for planning EME activity.
  ! Copyright by Joe Taylor, K1JT, and licensed for use by anyone
  ! under terms of the General Public License, GPLv3.

  ! In a bash shell, with gfortran and gnuplot accessible in your PATH:
  ! $ gfortran -o EMEchart -static EMEchart.f90
  ! $ 

  character arg*8,line*120,cmnd*20,path*256
  integer moday(12)
  logical leap,lsun,lsgr
  data moday/31,28,31,30,31,30,31,31,30,31,30,32/  !Include Jan of next year
  data rasgr/266.8/,decsgr/-29.0/                  !RA, Dec of Sgr A

  narg=iargc()
  if(narg.ne.1) then
     print*,'Usage: EMEchart <year>'
     go to 999
  endif
  call getarg(1,arg)
  read(arg,*) nyear
  leap=.false.
  if(mod(nyear,4).eq.0) then
     leap=.true.                               !This is a leap year
     moday(2)=29
  endif

  open(10,file='EMEchart.in',status='old')      !Bare bones gnuplot script
  open(11,file='EMEchart.plt',status='unknown') !Finished gnuplot script
  write(11,1000) nyear
1000 format('set output "EMEchart_',i4,'.pdf"')
  write(11,1002) nyear
1002 format('set title "Moon Ephemeris for ',i4,  &
          ', by K1JT" offset 0,2 font "Sans Bold,16"')

  id=0                                         !id is day of year
  imo1=1
  do i=1,999
     read(10,'(a120)',end=999) line
     if(leap .and. (line(1:17).eq.'set xrange [1:91]')) line='set xrange [1:92]'
     if(leap .and. (line(1:19).eq.'set xrange [91:182]')) line='set xrange [92:183]'
     if(leap .and. (line(1:20).eq.'set xrange [182:274]')) line='set xrange [183:275]'
     if(leap .and. (line(1:20).eq.'set xrange [274:366]')) line='set xrange [275:367]'
     write(11,'(a)') trim(line)
     if(line(1:9).eq.'# Weekend') then
        call labobj(imo1,imo1+2)
        imo1=imo1+3
     endif
  enddo
  
999 continue

contains

subroutine labobj(imo1,imo2)

  character line*256,ticpos*7

  nlab=10
  nobj=0
  line=''
  do imo=imo1,imo2
     do iday=1,moday(imo)
        id=id+1
        do iut=0,1
           uth=iut*12.0
           call sun0(nyear,1,id,uth,mjd,rasun,decsun)
           call moon0(nyear,1,id,uth,ramoon,decmoon,dist)
           extra_loss=10.0*log10((356000.0/dist)**4)
           x=rasun-ramoon
           if(x.gt.180.0) x=x-360.0
           if(x.lt.-180.0) x=x+360.0
           x=x/cos((decsun+decmoon)/(2.0*57.2957795))
           y=decsun-decmoon
           sunmoon=sqrt(x*x + y*y)
           x=rasgr-ramoon
           if(x.gt.180.0) x=x-360.0
           if(x.lt.-180.0) x=x+360.0
           x=x/cos((decsun+decmoon)/(2.0*57.2957795))
           y=decsgr-decmoon
           sgrmoon=sqrt(x*x + y*y)
           lsun=sunmoon.lt.10.0
           lsgr=sgrmoon.lt.10.0
           day=id + uth/24.0
           write(14,1010) day,nyear,imo,iday,decmoon,extra_loss,sunmoon,sgrmoon
1010       format(f6.2,i6,2i3.2,4f8.2)
           if(lsun)  write(15,1020) day,decmoon
1020       format(f6.2,f7.2)
           if(lsgr)  write(16,1020) day,decmoon
           if(iut.eq.0 .and. mod(mjd,7).eq.3) then
              nlab=nlab+1
              if(iday.lt.10) write(11,1030) nlab,iday,id
1030          format('set label',i3,' "',i1,'" at',i4,',32 center font "Sans,10"')
              if(iday.ge.10) write(11,1040) nlab,iday,id
1040          format('set label',i3,' "',i2,'" at',i4,',32 center font "Sans,10"')
              nobj=nobj+1
              write(11,1050) nobj,id+1
1050          format('set object',i3,' rectangle center',i4,  &
                   ',28.0 size 2,4 fc rgb "green" fs solid 0.5')
              write(ticpos,1060) id
1060          format('""',i4,',')
              line=trim(line)//ticpos
              write(ticpos,1060) id+2
              line=trim(line)//ticpos
           endif
        enddo
     enddo
  enddo
  n=len(trim(line))
  line(n:n)=')'
  line='set xtics ('//trim(line)
  write(11,'(a)') trim(line)

  return
end subroutine labobj

end program EMEchart

subroutine sun0(y,m,DD,UT,mjd,RA,Dec)

  implicit none

  integer y                         !Year
  integer m                         !Month
  integer DD                        !Day
  integer mjd                       !Modified Julian Date
  real UT                           !UTC in hours
  real RA,Dec                       !RA and Dec of sun

! NB: Double caps here are single caps in the writeup.

! Orbital elements of the Sun (also N=0, i=0, a=1):
  real w                            !Argument of perihelion
  real e                            !Eccentricity
  real MM                           !Mean anomaly
  real Ls                           !Mean longitude

! Other standard variables:
  real v                            !True anomaly
  real EE                           !Eccentric anomaly
  real ecl                          !Obliquity of the ecliptic
  real d                            !Ephemeris time argument in days
  real r                            !Distance to sun, AU
  real xv,yv                        !x and y coords in ecliptic
  real lonsun                       !Ecliptic long and lat of sun
! Ecliptic coords of sun (geocentric)
  real xs,ys
! Equatorial coords of sun (geocentric)
  real xe,ye,ze
  real rad
  data rad/57.2957795/

! Time in days, with Jan 0, 2000 equal to 0.0:
  d=367*y - 7*(y+(m+9)/12)/4 + 275*m/9 + DD - 730530 + UT/24.0
  mjd=d + 51543
  ecl = 23.4393 - 3.563e-7 * d

! Compute updated orbital elements for Sun:
  w = 282.9404 + 4.70935e-5 * d
  e = 0.016709 - 1.151e-9 * d
  MM = mod(356.0470d0 + 0.9856002585d0 * d + 360000.d0,360.d0)
  Ls = mod(w+MM+720.0,360.0)

  EE = MM + e*rad*sin(MM/rad) * (1.0 + e*cos(M/rad))
  EE = EE - (EE - e*rad*sin(EE/rad)-MM) / (1.0 - e*cos(EE/rad))

  xv = cos(EE/rad) - e
  yv = sqrt(1.0-e*e) * sin(EE/rad)
  v = rad*atan2(yv,xv)
  r = sqrt(xv*xv + yv*yv)
  lonsun = mod(v + w + 720.0,360.0)
! Ecliptic coordinates of sun (rectangular):
  xs = r * cos(lonsun/rad)
  ys = r * sin(lonsun/rad)

! Equatorial coordinates of sun (rectangular):
  xe = xs
  ye = ys * cos(ecl/rad)
  ze = ys * sin(ecl/rad)

! RA and Dec in degrees:
  RA = rad*atan2(ye,xe)
  Dec = rad*atan2(ze,sqrt(xe*xe + ye*ye))

  return
end subroutine sun0

subroutine moon0(y,m,Day,UT4,RA4,Dec4,dist4)

  implicit none

  integer y                           !Year
  integer m                           !Month
  integer Day                         !Day
  real*4 UT4,RA4,Dec4,dist4
  real*8 UT                           !UTC in hours
  real*8 RA,Dec                       !RA and Dec of moon

! NB: Double caps are single caps in the writeup.

  real*8 NN                           !Longitude of ascending node
  real*8 i                            !Inclination to the ecliptic
  real*8 w                            !Argument of perigee
  real*8 a                            !Semi-major axis
  real*8 e                            !Eccentricity
  real*8 MM                           !Mean anomaly

  real*8 v                            !True anomaly
  real*8 EE                           !Eccentric anomaly
  real*8 ecl                          !Obliquity of the ecliptic

  real*8 d                            !Ephemeris time argument in days
  real*8 r                            !Distance to sun, AU
  real*8 xv,yv                        !x and y coords in ecliptic
  real*8 lonecl,latecl                !Ecliptic long and lat of moon
  real*8 xg,yg,zg                     !Ecliptic rectangular coords
  real*8 Ms                           !Mean anomaly of sun
  real*8 ws                           !Argument of perihelion of sun
  real*8 Ls                           !Mean longitude of sun (Ns=0)
  real*8 Lm                           !Mean longitude of moon
  real*8 DD                           !Mean elongation of moon
  real*8 FF                           !Argument of latitude for moon
  real*8 xe,ye,ze                     !Equatorial geocentric coords of moon
  real*8 dist
  real*8 rad,twopi
  data rad/57.2957795131d0/,twopi/6.283185307d0/

  UT=UT4
  d=367*y - 7*(y+(m+9)/12)/4 + 275*m/9 + Day - 730530 + UT/24.d0
  ecl = 23.4393d0 - 3.563d-7 * d

! Orbital elements for Moon:  
  NN = 125.1228d0 - 0.0529538083d0 * d
  i = 5.1454d0
  w = mod(318.0634d0 + 0.1643573223d0 * d + 360000.d0,360.d0)
  a = 60.2666d0
  e = 0.054900d0
  MM = mod(115.3654d0 + 13.0649929509d0 * d + 360000.d0,360.d0)

  EE = MM + e*rad*sin(MM/rad) * (1.d0 + e*cos(MM/rad))
  EE = EE - (EE - e*rad*sin(EE/rad)-MM) / (1.d0 - e*cos(EE/rad))
  EE = EE - (EE - e*rad*sin(EE/rad)-MM) / (1.d0 - e*cos(EE/rad))

  xv = a * (cos(EE/rad) - e)
  yv = a * (sqrt(1.d0-e*e) * sin(EE/rad))

  v = mod(rad*atan2(yv,xv)+720.d0,360.d0)
  r = sqrt(xv*xv + yv*yv)

! Get geocentric position in ecliptic rectangular coordinates:

  xg = r * (cos(NN/rad)*cos((v+w)/rad)-sin(NN/rad)*sin((v+w)/rad)*cos(i/rad))
  yg = r * (sin(NN/rad)*cos((v+w)/rad)+cos(NN/rad)*sin((v+w)/rad)*cos(i/rad))
  zg = r * (sin((v+w)/rad)*sin(i/rad))

! Ecliptic longitude and latitude of moon:
  lonecl = mod(rad*atan2(yg/rad,xg/rad)+720.d0,360.d0)
  latecl = rad*atan2(zg/rad,sqrt(xg*xg + yg*yg)/rad)

! Now include orbital perturbations:
  Ms = mod(356.0470d0 + 0.9856002585d0 * d + 3600000.d0,360.d0)
  ws = 282.9404d0 + 4.70935d-5*d
  Ls = mod(Ms + ws + 720.d0,360.d0)
  Lm = mod(MM + w + NN+720.d0,360.d0)
  DD = mod(Lm - Ls + 360.d0,360.d0)
  FF = mod(Lm - NN + 360.d0,360.d0)

  lonecl = lonecl                                &
       - 1.274d0 * sin((MM-2.d0*DD)/rad)         &
       + 0.658d0 * sin(2.d0*DD/rad)              &
       - 0.186d0 * sin(Ms/rad)                   &
       - 0.059d0 * sin((2.d0*MM-2.d0*DD)/rad)    &
       - 0.057d0 * sin((MM-2.d0*DD+Ms)/rad)      &
       + 0.053d0 * sin((MM+2.d0*DD)/rad)         &
       + 0.046d0 * sin((2.d0*DD-Ms)/rad)         &
       + 0.041d0 * sin((MM-Ms)/rad)              &
       - 0.035d0 * sin(DD/rad)                   &
       - 0.031d0 * sin((MM+Ms)/rad)              &
       - 0.015d0 * sin((2.d0*FF-2.d0*DD)/rad)    &
       + 0.011d0 * sin((MM-4.d0*DD)/rad)

  latecl = latecl                                &
       - 0.173d0 * sin((FF-2.d0*DD)/rad)         &
       - 0.055d0 * sin((MM-FF-2.d0*DD)/rad)      &
       - 0.046d0 * sin((MM+FF-2.d0*DD)/rad)      &
       + 0.033d0 * sin((FF+2.d0*DD)/rad)         &
       + 0.017d0 * sin((2.d0*MM+FF)/rad)

  r = 60.36298d0                                 &
       - 3.27746d0*cos(MM/rad)                   &
       - 0.57994d0*cos((MM-2.d0*DD)/rad)         &
       - 0.46357d0*cos(2.d0*DD/rad)              &
       - 0.08904d0*cos(2.d0*MM/rad)              &
       + 0.03865d0*cos((2.d0*MM-2.d0*DD)/rad)    &
       - 0.03237d0*cos((2.d0*DD-Ms)/rad)         &
       - 0.02688d0*cos((MM+2.d0*DD)/rad)         &
       - 0.02358d0*cos((MM-2.d0*DD+Ms)/rad)      &
       - 0.02030d0*cos((MM-Ms)/rad)              &
       + 0.01719d0*cos(DD/rad)                   &
       + 0.01671d0*cos((MM+Ms)/rad)
  
  dist=r*6378.140d0

! Geocentric coordinates:
! Rectangular ecliptic coordinates of the moon:

  xg = r * cos(lonecl/rad)*cos(latecl/rad)
  yg = r * sin(lonecl/rad)*cos(latecl/rad)
  zg = r *                 sin(latecl/rad)

! Rectangular equatorial coordinates of the moon:
  xe = xg
  ye = yg*cos(ecl/rad) - zg*sin(ecl/rad)
  ze = yg*sin(ecl/rad) + zg*cos(ecl/rad)
   
! Right Ascension, Declination:
  RA = mod(rad*atan2(ye,xe)+360.d0,360.d0)
  Dec = rad*atan2(ze,sqrt(xe*xe + ye*ye))

  RA4=RA
  Dec4=Dec
  dist4=dist

  return
end subroutine moon0
