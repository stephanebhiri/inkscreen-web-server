# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True

import numpy as np
cimport numpy as np
cimport cython

np.import_array()

@cython.boundscheck(False)
@cython.wraparound(False)
def sierra_sorbet_dither(np.ndarray[np.float32_t, ndim=3] img_array, 
                         np.ndarray[np.float32_t, ndim=2] palette):
    """Sierra dithering SORBET - Compromis parfait entre corrections et fidélité"""
    cdef int h = img_array.shape[0]
    cdef int w = img_array.shape[1]
    cdef int palette_size = palette.shape[0]
    
    cdef np.ndarray[np.uint8_t, ndim=2] result = np.zeros((h, w), dtype=np.uint8)
    cdef float r, g, b, gray
    cdef float er, eg, eb
    cdef float dist, min_dist
    cdef int x, y, i, nearest_idx
    
    # PONDÉRATION SORBET - Compromis entre standard et correction forte
    cdef float weight_r = 0.299
    cdef float weight_g = 0.587  
    cdef float weight_b = 0.095  # Entre 0.114 (vanilla) et 0.08 (fort)
    
    for y in range(h):
        for x in range(w):
            r = img_array[y, x, 0]
            g = img_array[y, x, 1]
            b = img_array[y, x, 2]
            
            # Calcul du gris pour seuils
            gray = r * 0.299 + g * 0.587 + b * 0.114
            
            # SEUILS LÉGERS - Moins agressifs
            if gray > 250:  # Seulement zones TRÈS claires
                nearest_idx = 1  # WHITE
            elif gray < 5:  # Seulement zones TRÈS sombres
                nearest_idx = 0  # BLACK
            else:
                # Trouver couleur la plus proche
                min_dist = 1e10
                nearest_idx = 0
                
                for i in range(palette_size):
                    dist = ((palette[i,0] - r) * weight_r) ** 2 + \
                           ((palette[i,1] - g) * weight_g) ** 2 + \
                           ((palette[i,2] - b) * weight_b) ** 2
                    
                    # PÉNALITÉ LÉGÈRE du bleu dans zones claires
                    if i == 4 and gray > 200:  # Seuil plus élevé
                        dist += 400  # Pénalité modérée (20² au lieu de 50²)
                    
                    if dist < min_dist:
                        min_dist = dist
                        nearest_idx = i
            
            result[y, x] = nearest_idx
            
            # Calculer erreur
            er = r - palette[nearest_idx, 0]
            eg = g - palette[nearest_idx, 1]
            eb = b - palette[nearest_idx, 2]
            
            # RÉDUCTION MODÉRÉE de diffusion pour le bleu
            if nearest_idx == 4:  # Si pixel bleu
                er *= 0.7  # 70% au lieu de 50%
                eg *= 0.7
                eb *= 0.7
            
            # SIERRA PATTERN avec coefficients LÉGÈREMENT réduits
            #     X  4.5  2.8
            #  1.8  3.8  4.5  3.8  1.8
            #     1.8  2.8  1.8
            
            # Ligne actuelle
            if x + 1 < w:
                img_array[y, x+1, 0] = min(255, max(0, img_array[y, x+1, 0] + er * 4.5/32))
                img_array[y, x+1, 1] = min(255, max(0, img_array[y, x+1, 1] + eg * 4.5/32))
                img_array[y, x+1, 2] = min(255, max(0, img_array[y, x+1, 2] + eb * 4.5/32))
            
            if x + 2 < w:
                img_array[y, x+2, 0] = min(255, max(0, img_array[y, x+2, 0] + er * 2.8/32))
                img_array[y, x+2, 1] = min(255, max(0, img_array[y, x+2, 1] + eg * 2.8/32))
                img_array[y, x+2, 2] = min(255, max(0, img_array[y, x+2, 2] + eb * 2.8/32))
            
            # Ligne suivante
            if y + 1 < h:
                if x - 2 >= 0:
                    img_array[y+1, x-2, 0] = min(255, max(0, img_array[y+1, x-2, 0] + er * 1.8/32))
                    img_array[y+1, x-2, 1] = min(255, max(0, img_array[y+1, x-2, 1] + eg * 1.8/32))
                    img_array[y+1, x-2, 2] = min(255, max(0, img_array[y+1, x-2, 2] + eb * 1.8/32))
                
                if x - 1 >= 0:
                    img_array[y+1, x-1, 0] = min(255, max(0, img_array[y+1, x-1, 0] + er * 3.8/32))
                    img_array[y+1, x-1, 1] = min(255, max(0, img_array[y+1, x-1, 1] + eg * 3.8/32))
                    img_array[y+1, x-1, 2] = min(255, max(0, img_array[y+1, x-1, 2] + eb * 3.8/32))
                
                img_array[y+1, x, 0] = min(255, max(0, img_array[y+1, x, 0] + er * 4.5/32))
                img_array[y+1, x, 1] = min(255, max(0, img_array[y+1, x, 1] + eg * 4.5/32))
                img_array[y+1, x, 2] = min(255, max(0, img_array[y+1, x, 2] + eb * 4.5/32))
                
                if x + 1 < w:
                    img_array[y+1, x+1, 0] = min(255, max(0, img_array[y+1, x+1, 0] + er * 3.8/32))
                    img_array[y+1, x+1, 1] = min(255, max(0, img_array[y+1, x+1, 1] + eg * 3.8/32))
                    img_array[y+1, x+1, 2] = min(255, max(0, img_array[y+1, x+1, 2] + eb * 3.8/32))
                
                if x + 2 < w:
                    img_array[y+1, x+2, 0] = min(255, max(0, img_array[y+1, x+2, 0] + er * 1.8/32))
                    img_array[y+1, x+2, 1] = min(255, max(0, img_array[y+1, x+2, 1] + eg * 1.8/32))
                    img_array[y+1, x+2, 2] = min(255, max(0, img_array[y+1, x+2, 2] + eb * 1.8/32))
            
            # Ligne d'après
            if y + 2 < h:
                if x - 1 >= 0:
                    img_array[y+2, x-1, 0] = min(255, max(0, img_array[y+2, x-1, 0] + er * 1.8/32))
                    img_array[y+2, x-1, 1] = min(255, max(0, img_array[y+2, x-1, 1] + eg * 1.8/32))
                    img_array[y+2, x-1, 2] = min(255, max(0, img_array[y+2, x-1, 2] + eb * 1.8/32))
                
                img_array[y+2, x, 0] = min(255, max(0, img_array[y+2, x, 0] + er * 2.8/32))
                img_array[y+2, x, 1] = min(255, max(0, img_array[y+2, x, 1] + eg * 2.8/32))
                img_array[y+2, x, 2] = min(255, max(0, img_array[y+2, x, 2] + eb * 2.8/32))
                
                if x + 1 < w:
                    img_array[y+2, x+1, 0] = min(255, max(0, img_array[y+2, x+1, 0] + er * 1.8/32))
                    img_array[y+2, x+1, 1] = min(255, max(0, img_array[y+2, x+1, 1] + eg * 1.8/32))
                    img_array[y+2, x+1, 2] = min(255, max(0, img_array[y+2, x+1, 2] + eb * 1.8/32))
    
    return result